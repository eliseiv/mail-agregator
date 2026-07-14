"""Prod-bug regression (2026-07-15) — the poisoned ``smtp_username`` is written NULL.

Source of truth: ``backend/app/accounts/service.py`` — ``create`` (persists the
scrubbed ``smtp_username``) and ``update`` (self-heal: ``new_smtp_username =
normalize_optional_login(acc.smtp_username)`` rewrites a stored ``'None'`` to SQL
NULL on any legitimate PATCH). ``docs/03-data-model.md`` documents
``smtp_username`` as *nullable, falls back to email*.

These two cases touch a REAL Postgres (via the shared ``db_session`` fixture,
which SKIPS when PG is unreachable — so this file stays green in an infra-free
``pytest tests/unit`` locally, and runs for real in CI where the service
containers are up). Everything runs against the actual ``mail_accounts`` table:

- **create (case 7):** a create carrying ``smtp_username='None'`` lands SQL NULL
  in the column (the schema scrubs it to ``None``, the writer persists NULL —
  proven by reading the row back, not by trusting the DTO).
- **self-heal (case 9):** a row PRE-POISONED with the literal text ``'None'``
  (inserted RAW, bypassing the schema — exactly how the 41 prod rows got there)
  is rewritten to SQL NULL by an ordinary ``display_name`` PATCH.

The connectivity probe that ``create`` runs is stubbed (``imap_test_login`` /
``smtp_test_login``) — this is a persistence test, not a network one.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.accounts import service as acct_svc
from backend.app.accounts.schemas import MailAccountCreateRequest, MailAccountUpdateRequest
from backend.app.accounts.service import MailAccountService
from backend.app.deps import VisibilityScope
from backend.app.repositories.mail_accounts import MailAccountsRepo
from shared.crypto import encrypt_mail_password
from shared.models import User

pytestmark = pytest.mark.unit

_CREDS: dict[str, Any] = {
    "email": "poisoned@example.com",
    "password": "imap-pw",
    "imap_host": "imap.example.com",
    "imap_port": 993,
    "imap_ssl": True,
    "smtp_host": "smtp.example.com",
    "smtp_port": 465,
    "smtp_ssl": True,
    "smtp_starttls": False,
}


async def _seed_owner(session: AsyncSession) -> User:
    user = User(username="crm-service-persist-test", role="super_admin", display_name="CRM")
    session.add(user)
    await session.flush()
    await session.refresh(user)
    return user


def _scope(user_id: int) -> VisibilityScope:
    return VisibilityScope(
        user_id=user_id, role="super_admin", group_id=None, group_ids=frozenset()
    )


@pytest.fixture
def stub_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """No-op the IMAP/SMTP connectivity probe ``create`` runs (offline test)."""

    async def _ok(**_k: Any) -> None:
        return None

    monkeypatch.setattr(acct_svc, "imap_test_login", _ok)
    monkeypatch.setattr(acct_svc, "smtp_test_login", _ok)


class TestCreatePersistsNull:
    async def test_create_with_none_username_writes_sql_null(
        self, db_session: AsyncSession, stub_probe: None
    ) -> None:
        owner = await _seed_owner(db_session)
        svc = MailAccountService(db_session)

        dto = await svc.create(
            scope=_scope(owner.id),
            payload=MailAccountCreateRequest(**_CREDS, smtp_username="None"),
        )

        # Read the ROW back — the source of truth is the column, not the DTO.
        row = await MailAccountsRepo(db_session).get_by_id(dto.id)
        assert row is not None
        assert row.smtp_username is None

    async def test_create_with_real_username_persists_it(
        self, db_session: AsyncSession, stub_probe: None
    ) -> None:
        owner = await _seed_owner(db_session)
        svc = MailAccountService(db_session)

        dto = await svc.create(
            scope=_scope(owner.id),
            payload=MailAccountCreateRequest(
                **{**_CREDS, "email": "real@example.com"}, smtp_username="postmaster@example.com"
            ),
        )
        row = await MailAccountsRepo(db_session).get_by_id(dto.id)
        assert row is not None
        assert row.smtp_username == "postmaster@example.com"


class TestSelfHealPatch:
    async def test_legit_patch_rewrites_stored_none_text_to_sql_null(
        self, db_session: AsyncSession
    ) -> None:
        owner = await _seed_owner(db_session)
        repo = MailAccountsRepo(db_session)

        # Pre-poison the row EXACTLY like the prod import: the literal 4-char text
        # 'None' (not SQL NULL) inserted RAW, bypassing the schema scrub.
        new_id = await repo.next_account_id()
        acc = await repo.insert_with_id(
            account_id=new_id,
            user_id=owner.id,
            email="poisoned@example.com",
            encrypted_password=encrypt_mail_password("imap-pw", new_id),
            imap_host="imap.example.com",
            imap_port=993,
            imap_ssl=True,
            smtp_host="smtp.example.com",
            smtp_port=465,
            smtp_ssl=True,
            smtp_starttls=False,
            smtp_username="None",  # <-- the poison
            smtp_encrypted_password=None,
        )
        assert acc.smtp_username == "None"  # confirm the row is poisoned

        # An ordinary, credential-untouched PATCH (rename only) must self-heal it.
        svc = MailAccountService(db_session)
        await svc.update(
            scope=_scope(owner.id),
            account_id=new_id,
            payload=MailAccountUpdateRequest(display_name="Renamed"),
        )

        healed = await repo.get_by_id(new_id)
        assert healed is not None
        assert healed.smtp_username is None  # rewritten to SQL NULL
        assert healed.display_name == "Renamed"  # the actual edit landed
