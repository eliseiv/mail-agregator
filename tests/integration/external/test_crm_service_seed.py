"""``crm-service`` technical user seed (ADR-0039 §Q-0039-1).

The owner of all externally-created mailboxes. Invariants asserted here against
real Postgres:

- ``seed_crm_service_user`` is idempotent (``created`` → ``unchanged``);
- a concurrent double-seed (two workers booting together) is race-safe: the
  loser's ``IntegrityError`` is caught inside its SAVEPOINT and never breaks the
  shared lifespan transaction — exactly one row ends up present;
- ``crm-service`` is a ``super_admin`` with ``group_id IS NULL`` and NO login
  password;
- it NEVER resolves as a notification recipient — it has no ``telegram_links``
  row and the recipient SQL INNER-JOINs ``telegram_links`` (so a super_admin
  with no link is filtered out), verified via ``list_recipients_for_message``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from backend.app.auth.service import CRM_SERVICE_USERNAME, seed_crm_service_user
from backend.app.repositories.telegram_notifications import TelegramNotificationsRepo
from shared.models import TelegramLink, User

pytestmark = pytest.mark.integration


async def _seed_in_tx(db_engine: AsyncEngine) -> str:
    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with factory() as ses, ses.begin():
        return await seed_crm_service_user(ses)


async def _count_crm(db_engine: AsyncEngine) -> int:
    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with factory() as ses:
        rows = (
            (await ses.execute(select(User).where(User.username == CRM_SERVICE_USERNAME)))
            .scalars()
            .all()
        )
    return len(rows)


class TestSeedIdempotency:
    async def test_second_seed_is_unchanged(self, db_engine: AsyncEngine) -> None:
        # Truncate (autouse) removed the lifespan-seeded row: first call creates.
        first = await _seed_in_tx(db_engine)
        second = await _seed_in_tx(db_engine)
        assert first == "created"
        assert second == "unchanged"
        assert await _count_crm(db_engine) == 1

    async def test_seeded_row_invariants(self, db_engine: AsyncEngine) -> None:
        await _seed_in_tx(db_engine)
        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as ses:
            u = (
                await ses.execute(select(User).where(User.username == CRM_SERVICE_USERNAME))
            ).scalar_one()
        assert u.role == "super_admin"
        assert u.group_id is None
        assert u.password_hash is None, "crm-service has no interactive login password"


class TestConcurrentSeed:
    async def test_concurrent_double_seed_is_race_safe(self, db_engine: AsyncEngine) -> None:
        """Two seeds racing on the ``username`` UNIQUE: the loser's IntegrityError
        is swallowed in its SAVEPOINT (no exception escapes), exactly one row
        remains, and at least one call reports ``created``."""
        results = await asyncio.gather(
            _seed_in_tx(db_engine), _seed_in_tx(db_engine), return_exceptions=True
        )
        # No call raised — the race is fully absorbed.
        assert all(not isinstance(r, BaseException) for r in results), results
        assert "created" in results
        assert await _count_crm(db_engine) == 1


class TestNotARecipient:
    async def test_crm_service_never_a_notification_recipient(
        self,
        client: Any,  # app lifespan seeds super_admin + crm-service
        db_engine: AsyncEngine,
        super_admin: User,
        crm_service_user: User,
        make_mail_account: Callable[..., Any],
        make_message: Callable[..., Any],
    ) -> None:
        """A message visible to both super_admins: only the one with a live
        ``telegram_links`` row (the real super_admin) resolves; ``crm-service``
        (no link) is filtered out by the recipient SQL's INNER JOIN."""
        # Real super_admin gets a live Telegram link created in the past so the
        # ``m.internal_date >= tl.created_at`` guard admits the message.
        past = datetime.now(UTC) - timedelta(hours=1)
        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as ses, ses.begin():
            ses.add(
                TelegramLink(
                    telegram_user_id=555001,
                    user_id=super_admin.id,
                    created_at=past,
                    dead_at=None,
                )
            )

        acc = await make_mail_account(super_admin.id, "crm-recip@example.com")
        msg = await make_message(acc.id, uid=1, internal_date=datetime.now(UTC))

        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as ses:
            recipients = await TelegramNotificationsRepo(ses).list_recipients_for_message(
                message_id=msg.id
            )
        recipient_ids = {r.user_id for r in recipients}
        assert super_admin.id in recipient_ids, "the linked super_admin must resolve"
        assert crm_service_user.id not in recipient_ids, "crm-service must never be a recipient"
        # Confirm crm-service genuinely has no telegram_links row.
        async with factory() as ses:
            n = (
                await ses.execute(
                    text("SELECT count(*) FROM telegram_links WHERE user_id = :u"),
                    {"u": crm_service_user.id},
                )
            ).scalar_one()
        assert n == 0
