"""Integration tests for ``MailAccountService`` — direct service calls
with real Postgres + Redis + MinIO; only IMAP/SMTP testers are mocked.

These complement ``test_accounts_crud.py`` (which exercises the full HTTP
stack) by hitting the service paths that the router doesn't always touch:
update with partial fields, force_sync marker, delete-with-blob-cleanup,
ConflictError on duplicate insert.

Source of truth: ``backend/app/accounts/service.py`` +
``docs/05-modules.md`` sec. 5.
"""

from __future__ import annotations

from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.accounts.schemas import (
    MailAccountCreateRequest,
    MailAccountUpdateRequest,
)
from backend.app.accounts.service import MailAccountService
from backend.app.exceptions import ConflictError, NotFoundError
from backend.app.repositories.users import UsersRepo
from shared.crypto import decrypt_mail_password
from shared.redis_client import get_redis

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _mock_test_login(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the IMAP/SMTP test-login coroutines so the network is never touched.

    Individual tests can re-monkeypatch to raise.
    """
    from backend.app.accounts import service as svc_mod

    async def _ok(**_: Any) -> None:
        return None

    monkeypatch.setattr(svc_mod, "imap_test_login", _ok)
    monkeypatch.setattr(svc_mod, "smtp_test_login", _ok)


@pytest_asyncio.fixture
async def user_id(db_session: AsyncSession) -> int:
    """Create one ordinary user and return its id."""
    repo = UsersRepo(db_session)
    user = await repo.create(
        username="alice",
        email="alice@example.com",
        is_admin=False,
        password_hash="x",
        password_reset_required=False,
    )
    await db_session.commit()
    return user.id


def _create_payload(**overrides: Any) -> MailAccountCreateRequest:
    """Default valid create payload; overrides win."""
    base: dict[str, Any] = {
        "email": "user@example.com",
        "password": "secret-imap-pwd",
        "imap_host": "imap.example.com",
        "imap_port": 993,
        "imap_ssl": True,
        "smtp_host": "smtp.example.com",
        "smtp_port": 465,
        "smtp_ssl": True,
        "smtp_starttls": False,
    }
    base.update(overrides)
    return MailAccountCreateRequest(**base)


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


class TestCreate:
    async def test_create_returns_dto_without_secrets(
        self, db_session: AsyncSession, user_id: int
    ) -> None:
        svc = MailAccountService(db_session)
        async with db_session.begin():
            dto = await svc.create(user_id=user_id, payload=_create_payload())
        assert dto.email == "user@example.com"
        assert dto.id > 0
        # DTO must not include any password fields.
        assert not hasattr(dto, "encrypted_password")
        assert not hasattr(dto, "password")

    async def test_create_with_separate_smtp_credentials(
        self, db_session: AsyncSession, user_id: int
    ) -> None:
        svc = MailAccountService(db_session)
        async with db_session.begin():
            dto = await svc.create(
                user_id=user_id,
                payload=_create_payload(
                    smtp_username="smtp@example.com",
                    smtp_password="separate-smtp-pwd",
                ),
            )
        assert dto.smtp_username == "smtp@example.com"
        # Verify the stored row actually has separate encrypted blobs by
        # decrypting both with the account id as AAD.
        from backend.app.repositories.mail_accounts import MailAccountsRepo

        acc = await MailAccountsRepo(db_session).get_by_id(dto.id)
        assert acc is not None
        assert acc.smtp_encrypted_password is not None
        assert decrypt_mail_password(acc.smtp_encrypted_password, acc.id) == "separate-smtp-pwd"
        assert decrypt_mail_password(acc.encrypted_password, acc.id) == "secret-imap-pwd"

    async def test_create_duplicate_email_raises_conflict(
        self, db_session: AsyncSession, user_id: int
    ) -> None:
        svc = MailAccountService(db_session)
        async with db_session.begin():
            await svc.create(user_id=user_id, payload=_create_payload())
        # Second insert of same email -> ConflictError (case-insensitive
        # uniqueness).
        with pytest.raises(ConflictError):
            async with db_session.begin():
                await svc.create(
                    user_id=user_id,
                    payload=_create_payload(email="USER@example.com"),
                )

    async def test_create_imap_failure_does_not_persist_row(
        self,
        db_session: AsyncSession,
        user_id: int,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If IMAP test fails, no mail_account row should be created."""
        from backend.app.accounts import service as svc_mod
        from backend.app.exceptions import IMAPLoginFailedError

        async def _bad_imap(**_: Any) -> None:
            raise IMAPLoginFailedError("nope")

        monkeypatch.setattr(svc_mod, "imap_test_login", _bad_imap)

        svc = MailAccountService(db_session)
        with pytest.raises(IMAPLoginFailedError):
            async with db_session.begin():
                await svc.create(user_id=user_id, payload=_create_payload())

        # No row.
        rows = await svc.list_for_user(user_id)
        assert rows == []


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------


class TestUpdate:
    async def test_partial_update_keeps_unchanged_fields(
        self, db_session: AsyncSession, user_id: int
    ) -> None:
        svc = MailAccountService(db_session)
        async with db_session.begin():
            created = await svc.create(user_id=user_id, payload=_create_payload())
        # Patch only imap_port.
        async with db_session.begin():
            updated = await svc.update(
                user_id=user_id,
                account_id=created.id,
                payload=MailAccountUpdateRequest(imap_port=143),
            )
        assert updated.imap_port == 143
        # Unchanged fields preserved.
        assert updated.email == created.email
        assert updated.smtp_host == created.smtp_host

    async def test_update_password_re_encrypts_blob(
        self, db_session: AsyncSession, user_id: int
    ) -> None:
        from backend.app.repositories.mail_accounts import MailAccountsRepo

        repo = MailAccountsRepo(db_session)
        svc = MailAccountService(db_session)
        async with db_session.begin():
            created = await svc.create(user_id=user_id, payload=_create_payload())

        # Read inside a fresh implicit transaction; refresh to see latest.
        acc_before = await repo.get_by_id(created.id)
        assert acc_before is not None
        original_blob = bytes(acc_before.encrypted_password)
        # Expire so the next read after the UPDATE re-fetches.
        await db_session.commit()

        async with db_session.begin():
            await svc.update(
                user_id=user_id,
                account_id=created.id,
                payload=MailAccountUpdateRequest(password="new-imap-pwd"),
            )

        # Force ORM cache eviction so we don't see the stale blob.
        db_session.expire_all()
        acc_after = await repo.get_by_id(created.id)
        assert acc_after is not None
        new_blob = bytes(acc_after.encrypted_password)
        assert new_blob != original_blob
        assert decrypt_mail_password(new_blob, created.id) == "new-imap-pwd"

    async def test_update_resets_failures_and_reactivates(
        self, db_session: AsyncSession, user_id: int
    ) -> None:
        """A successful re-test on PATCH clears last_sync_error / failures
        and flips is_active back to True.
        """
        from backend.app.repositories.mail_accounts import MailAccountsRepo

        svc = MailAccountService(db_session)
        async with db_session.begin():
            created = await svc.create(user_id=user_id, payload=_create_payload())
        # Manually disable + flag failures.
        async with db_session.begin():
            await MailAccountsRepo(db_session).update_fields(
                created.id,
                is_active=False,
                last_sync_error="something",
                consecutive_failures=7,
            )

        async with db_session.begin():
            dto = await svc.update(
                user_id=user_id,
                account_id=created.id,
                payload=MailAccountUpdateRequest(imap_port=993),
            )
        assert dto.is_active is True
        assert dto.last_sync_error is None
        assert dto.consecutive_failures == 0

    async def test_update_unknown_account_raises_not_found(
        self, db_session: AsyncSession, user_id: int
    ) -> None:
        svc = MailAccountService(db_session)
        with pytest.raises(NotFoundError):
            async with db_session.begin():
                await svc.update(
                    user_id=user_id,
                    account_id=999_999,
                    payload=MailAccountUpdateRequest(imap_port=143),
                )

    async def test_update_ssl_and_starttls_both_true_rejected(
        self, db_session: AsyncSession, user_id: int
    ) -> None:
        svc = MailAccountService(db_session)
        async with db_session.begin():
            created = await svc.create(user_id=user_id, payload=_create_payload())
        # Build a payload that, after merging, would be smtp_ssl=true AND
        # smtp_starttls=true — should raise ConflictError per service.
        with pytest.raises(ConflictError):
            async with db_session.begin():
                await svc.update(
                    user_id=user_id,
                    account_id=created.id,
                    payload=MailAccountUpdateRequest(smtp_starttls=True),
                    # Note: smtp_ssl is True from the create payload.
                )


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


class TestDelete:
    async def test_delete_unknown_account_raises_not_found(
        self, db_session: AsyncSession, user_id: int
    ) -> None:
        svc = MailAccountService(db_session)
        with pytest.raises(NotFoundError):
            async with db_session.begin():
                await svc.delete(user_id=user_id, account_id=999_999)

    async def test_delete_removes_row_and_calls_storage(
        self,
        db_session: AsyncSession,
        user_id: int,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Spy on storage.delete_objects + delete_prefix.
        from shared.storage import Storage

        called_objects: list[list[str]] = []
        called_prefixes: list[str] = []

        async def _spy_objs(self: Storage, keys: list[str]) -> None:  # noqa: ARG001
            called_objects.append(list(keys))

        async def _spy_prefix(self: Storage, prefix: str) -> int:  # noqa: ARG001
            called_prefixes.append(prefix)
            return 0

        monkeypatch.setattr(Storage, "delete_objects", _spy_objs, raising=True)
        monkeypatch.setattr(Storage, "delete_prefix", _spy_prefix, raising=True)

        svc = MailAccountService(db_session)
        async with db_session.begin():
            created = await svc.create(user_id=user_id, payload=_create_payload())

        async with db_session.begin():
            await svc.delete(user_id=user_id, account_id=created.id)

        # Row gone.
        rows = await svc.list_for_user(user_id)
        assert rows == []
        # delete_prefix called with "{user}/{account}/" — NB: regardless of
        # whether there are attachment keys, the per-account prefix sweep
        # always runs.
        assert any(p == f"{user_id}/{created.id}/" for p in called_prefixes)


# ---------------------------------------------------------------------------
# Force sync marker (Redis)
# ---------------------------------------------------------------------------


class TestForceSync:
    async def test_force_sync_sets_redis_key_with_ttl(
        self, db_session: AsyncSession, user_id: int
    ) -> None:
        svc = MailAccountService(db_session)
        async with db_session.begin():
            created = await svc.create(user_id=user_id, payload=_create_payload())
        await svc.force_sync(user_id=user_id, account_id=created.id)

        r = get_redis()
        val = await r.get(f"force_sync:{created.id}")
        assert val == b"1" or val == "1"
        ttl = await r.ttl(f"force_sync:{created.id}")
        # 60s set; allow some clock slack.
        assert 1 <= ttl <= 60

    async def test_force_sync_unknown_account_raises_not_found(
        self, db_session: AsyncSession, user_id: int
    ) -> None:
        svc = MailAccountService(db_session)
        with pytest.raises(NotFoundError):
            await svc.force_sync(user_id=user_id, account_id=999_999)


# ---------------------------------------------------------------------------
# Cross-user isolation
# ---------------------------------------------------------------------------


class TestOwnership:
    async def test_other_users_account_invisible(
        self, db_session: AsyncSession, user_id: int
    ) -> None:
        # Create another user.
        other = await UsersRepo(db_session).create(
            username="bob",
            email=None,
            is_admin=False,
            password_hash="x",
            password_reset_required=False,
        )
        await db_session.commit()

        svc = MailAccountService(db_session)
        async with db_session.begin():
            alice_acc = await svc.create(user_id=user_id, payload=_create_payload())

        # Bob asks for Alice's account by id -> NotFound.
        with pytest.raises(NotFoundError):
            await svc.get_for_user(user_id=other.id, account_id=alice_acc.id)
        # Bob's listing is empty.
        assert await svc.list_for_user(other.id) == []
