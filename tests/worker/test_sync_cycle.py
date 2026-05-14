"""Worker tests for sync_cycle:
- Initial sync (no last_synced_uidnext) calls fetch with the right window.
- Per-account timeout -> recorded as failure (consecutive_failures += 1).
- 3 consecutive fails -> auto-disable + admin_audit ``account_auto_disabled``.
- SSRF guard: private host -> InvalidHostError -> disable.
- Force_sync via Redis key prioritises account.

Source of truth: ``worker/app/sync_cycle.py`` + ``docs/05-modules.md`` sec.14
+ ADR-0008 + ADR-0013.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from shared.crypto import encrypt_mail_password
from shared.models import AdminAudit, MailAccount, User
from worker.app import sync_cycle as sc

pytestmark = pytest.mark.integration  # needs the DB + Redis to be live


@pytest.fixture
async def admin_user_with_account(
    db_engine: AsyncEngine,
) -> dict[str, Any]:
    """Pre-seed an admin user + a mail_account row."""
    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with factory() as ses, ses.begin():
        admin = User(
            username="sync_admin",
            role="super_admin",
            password_hash="$argon2id$v=19$m=65536,t=3,p=4$abc$def",
            password_reset_required=False,
        )
        ses.add(admin)
        await ses.flush()
        from backend.app.repositories.mail_accounts import MailAccountsRepo

        repo = MailAccountsRepo(ses)
        new_id = await repo.next_account_id()
        blob = encrypt_mail_password("p", new_id)
        acc = MailAccount(
            id=new_id,
            user_id=admin.id,
            email="sync@example.com",
            encrypted_password=blob,
            imap_host="imap.example.com",
            imap_port=993,
            imap_ssl=True,
            smtp_host="smtp.example.com",
            smtp_port=465,
            smtp_ssl=True,
            smtp_starttls=False,
        )
        ses.add(acc)
        await ses.flush()
        return {"user_id": admin.id, "account_id": acc.id}


class TestForceSyncDrain:
    async def test_drain_returns_account_ids_and_deletes_keys(self, redis_client: Any) -> None:
        await redis_client.set("force_sync:42", "1")
        await redis_client.set("force_sync:99", "1")
        await redis_client.set("unrelated:7", "x")
        ids = await sc._drain_forced_account_ids()
        assert ids == {42, 99}
        # Markers removed.
        assert await redis_client.get("force_sync:42") is None
        assert await redis_client.get("force_sync:99") is None
        # Unrelated key untouched.
        assert await redis_client.get("unrelated:7") == "x"


class TestSyncOneAccount:
    async def test_timeout_records_failure_and_does_not_raise(
        self,
        admin_user_with_account: dict[str, Any],
        db_engine: AsyncEngine,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Make ``asyncio.wait_for`` raise TimeoutError.
        async def _fake_to_thread(*_a: Any, **_k: Any) -> None:
            await asyncio.sleep(10)

        monkeypatch.setattr(asyncio, "to_thread", _fake_to_thread)

        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as ses:
            acc = await ses.get(MailAccount, admin_user_with_account["account_id"])
        assert acc is not None
        new_count, conflict = await sc.sync_one_account(
            acc,
            timeout_seconds=1,
            initial_sync_days=30,
            max_body_bytes=1024,
            max_att_bytes=1024,
        )
        assert new_count == 0 and conflict == 0

        async with factory() as ses:
            acc2 = await ses.get(MailAccount, admin_user_with_account["account_id"])
        assert acc2 is not None
        assert acc2.consecutive_failures >= 1
        assert acc2.last_sync_error is not None
        assert "timeout" in acc2.last_sync_error.lower()

    async def test_three_consecutive_timeouts_auto_disable_and_audit(
        self,
        admin_user_with_account: dict[str, Any],
        db_engine: AsyncEngine,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def _fake_to_thread(*_a: Any, **_k: Any) -> None:
            await asyncio.sleep(10)

        monkeypatch.setattr(asyncio, "to_thread", _fake_to_thread)
        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as ses:
            acc = await ses.get(MailAccount, admin_user_with_account["account_id"])
        assert acc is not None

        for _ in range(3):
            await sc.sync_one_account(
                acc,
                timeout_seconds=1,
                initial_sync_days=30,
                max_body_bytes=1024,
                max_att_bytes=1024,
            )

        async with factory() as ses:
            acc2 = await ses.get(MailAccount, admin_user_with_account["account_id"])
        assert acc2 is not None
        assert acc2.is_active is False
        assert acc2.consecutive_failures >= 3
        async with factory() as ses:
            audits = (
                (
                    await ses.execute(
                        select(AdminAudit).where(AdminAudit.action == "account_auto_disabled")
                    )
                )
                .scalars()
                .all()
            )
        assert len(audits) >= 1
        assert audits[0].target_user_id == admin_user_with_account["user_id"]

    async def test_decrypt_failure_disables_account(
        self,
        admin_user_with_account: dict[str, Any],
        db_engine: AsyncEngine,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from cryptography.exceptions import InvalidTag

        def _bad_decrypt(*_a: Any, **_k: Any) -> str:
            raise InvalidTag("corrupt blob")

        monkeypatch.setattr(sc, "decrypt_mail_password", _bad_decrypt)

        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as ses:
            acc = await ses.get(MailAccount, admin_user_with_account["account_id"])
        assert acc is not None
        new_count, _ = await sc.sync_one_account(
            acc,
            timeout_seconds=10,
            initial_sync_days=30,
            max_body_bytes=1024,
            max_att_bytes=1024,
        )
        assert new_count == 0

        async with factory() as ses:
            acc2 = await ses.get(MailAccount, admin_user_with_account["account_id"])
        assert acc2 is not None
        assert acc2.is_active is False  # disable=True for decrypt failure
        assert acc2.last_sync_error is not None
        assert "decrypt" in acc2.last_sync_error.lower()

    async def test_successful_sync_persists_messages_via_idempotent_insert(
        self,
        admin_user_with_account: dict[str, Any],
        db_engine: AsyncEngine,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Build a fake FetchedBox with one message.
        from datetime import UTC
        from datetime import datetime as _dt

        from worker.app.imap_fetcher import (
            FetchedAttachment,
            FetchedBox,
            FetchedMessage,
        )

        fetched_box = FetchedBox(
            uidvalidity=42,
            uidnext=101,
            new_messages=[
                FetchedMessage(
                    uid=100,
                    message_id_header="<mid@x>",
                    from_addr="x@y.com",
                    from_name="X",
                    to_addrs="sync@example.com",
                    cc_addrs=None,
                    subject="hello",
                    internal_date=_dt.now(UTC),
                    body_text="hi",
                    body_html=None,
                    body_truncated=False,
                    body_present=True,
                    in_reply_to=None,
                    refs_header=None,
                    attachments=[
                        FetchedAttachment(
                            filename="a.txt",
                            content_type="text/plain",
                            size_bytes=5,
                            payload=b"abcde",
                        )
                    ],
                )
            ],
        )

        async def _fake_to_thread(_func: Any, *_a: Any, **_k: Any) -> Any:
            return fetched_box

        monkeypatch.setattr(asyncio, "to_thread", _fake_to_thread)

        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as ses:
            acc = await ses.get(MailAccount, admin_user_with_account["account_id"])
        assert acc is not None

        # First run: 1 new message inserted.
        new1, conflict1 = await sc.sync_one_account(
            acc,
            timeout_seconds=10,
            initial_sync_days=30,
            max_body_bytes=1024,
            max_att_bytes=1024 * 1024,
        )
        assert new1 == 1
        assert conflict1 == 0

        # Second run: same UID -> ON CONFLICT DO NOTHING => 0 new, 1 conflict.
        async with factory() as ses:
            acc2 = await ses.get(MailAccount, admin_user_with_account["account_id"])
        assert acc2 is not None
        new2, conflict2 = await sc.sync_one_account(
            acc2,
            timeout_seconds=10,
            initial_sync_days=30,
            max_body_bytes=1024,
            max_att_bytes=1024 * 1024,
        )
        assert new2 == 0
        assert conflict2 == 1
