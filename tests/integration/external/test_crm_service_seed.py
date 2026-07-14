"""``crm-service`` technical user seed (ADR-0039 §Q-0039-1).

The owner of all mailboxes. Invariants asserted here against real Postgres:

- ``seed_crm_service_user`` is idempotent (``created`` → ``unchanged``);
- a concurrent double-seed (two workers booting together) is race-safe: the
  loser's ``IntegrityError`` is caught inside its SAVEPOINT and never breaks the
  shared lifespan transaction — exactly one row ends up present;
- ``crm-service`` is a ``super_admin`` with NO login password.

ADR-0044 §1/§4: ``users`` survives as a TECHNICAL table carrying exactly this one row
(``mail_accounts.user_id`` is a NOT NULL FK with ``ON DELETE CASCADE``, so it cannot be
dropped). The "never a Telegram notification recipient" case went away with Telegram
(``telegram_links`` / ``telegram_notifications`` are decommissioned — there is nothing
left to be a recipient of), and ``users.group_id`` left the ORM mapping ahead of its
DDL drop (ADR-0044 §3 lock-step).
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from backend.app.auth.service import CRM_SERVICE_USERNAME, seed_crm_service_user
from shared.models import User

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
