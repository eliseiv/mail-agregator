"""Fixtures for the multi-group membership (ADR-0030) test suite.

These tests exercise the production SQL / repositories / services that read
``user_groups`` (the M:N source of truth introduced by ADR-0030) directly
against Postgres, using the function-scoped, rolled-back ``db_session``
fixture from the top-level ``tests/conftest.py`` (Postgres only — no redis /
minio / app lifespan).

The seeder is a thin extension of ``tests.tags.conftest.Seeder`` that knows
how to mirror the *home* membership in ``user_groups`` (which production does
in the migration backfill + ``AdminService``) and to add *additional*
memberships — so the visibility/notification predicates have something to
read.

Source of truth:
- ADR-0030 (multi-group membership) + docs/03-data-model.md (``user_groups``).
- docs/06-security.md §1.7/§1.9 (visibility consistent with notifications).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models import Group, TelegramLink, User, UserGroup
from tests.tags.conftest import Seeder


class MultiGroupSeeder(Seeder):
    """``Seeder`` that also writes ``user_groups`` rows (ADR-0030)."""

    async def membership(self, *, user_id: int, group_id: int) -> UserGroup:
        """Insert an explicit ``user_groups`` row (home or additional)."""
        ug = UserGroup(user_id=user_id, group_id=group_id)
        self.s.add(ug)
        await self.s.flush()
        return ug

    async def group_with_leader(self, name: str) -> tuple[Group, User]:
        """Override: mirror the leader's home membership in ``user_groups``."""
        g, leader = await super().group_with_leader(name)
        await self.membership(user_id=leader.id, group_id=g.id)
        return g, leader

    async def member(self, group_id: int) -> User:
        """Override: mirror the member's home membership in ``user_groups``."""
        u = await super().member(group_id)
        await self.membership(user_id=u.id, group_id=group_id)
        return u

    async def bare_group(self, name: str) -> Group:
        """A group whose leader we don't care about (still needs a leader FK).

        Creates a throwaway leader so ``groups.leader_user_id`` is satisfied,
        mirrors its home membership, and returns the group.
        """
        g, _ = await self.group_with_leader(name)
        return g

    async def telegram_link(
        self,
        *,
        user_id: int,
        telegram_user_id: int,
        created_at: datetime | None = None,
        dead_at: datetime | None = None,
    ) -> TelegramLink:
        link = TelegramLink(
            telegram_user_id=telegram_user_id,
            user_id=user_id,
            created_at=created_at or (datetime.now(UTC) - timedelta(hours=1)),
            dead_at=dead_at,
        )
        self.s.add(link)
        await self.s.flush()
        return link

    async def memberships_of(self, user_id: int) -> set[int]:
        rows = await self.s.execute(
            text("SELECT group_id FROM user_groups WHERE user_id = :uid"),
            {"uid": user_id},
        )
        return {int(r[0]) for r in rows}


@pytest_asyncio.fixture
async def mseed(db_session: AsyncSession) -> MultiGroupSeeder:
    return MultiGroupSeeder(db_session)
