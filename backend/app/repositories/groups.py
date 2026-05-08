"""Repository for ``groups`` (ADR-0019).

Group lifecycle (super-admin only):

- Auto-create on ``POST /api/admin/users role='group_leader'`` (one tx
  with INSERT user, INSERT groups, UPDATE users.group_id; the FK to
  groups is DEFERRABLE INITIALLY DEFERRED so the order is permissive).
- Manual create on ``POST /api/admin/groups`` with ``leader_user_id``.
- Rename via ``PATCH /api/admin/groups/{id}``.
- Delete via ``DELETE /api/admin/groups/{id}`` — backend enforces "no
  members and no leader" before the SQL DELETE; ``ON DELETE RESTRICT``
  on ``groups.leader_user_id`` is a safety-net at the DB layer too.

Methods here are pure data access; business invariants live in
:class:`backend.app.groups.service.GroupsService`.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models import Group, User


class GroupsRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    # --- Reads -------------------------------------------------------------

    async def get_by_id(self, group_id: int) -> Group | None:
        return await self._s.get(Group, group_id)

    async def get_by_leader(self, leader_user_id: int) -> Group | None:
        stmt = select(Group).where(Group.leader_user_id == leader_user_id)
        return (await self._s.execute(stmt)).scalar_one_or_none()

    async def list_all(self, *, q: str | None, page: int, limit: int) -> tuple[list[Group], int]:
        stmt = select(Group).order_by(Group.id)
        count_stmt = select(func.count()).select_from(Group)
        if q:
            pattern = f"%{q.lower()}%"
            stmt = stmt.where(func.lower(Group.name).like(pattern))
            count_stmt = count_stmt.where(func.lower(Group.name).like(pattern))
        total = (await self._s.execute(count_stmt)).scalar_one()
        page = max(page, 1)
        limit = max(min(limit, 200), 1)
        stmt = stmt.offset((page - 1) * limit).limit(limit)
        items = list((await self._s.execute(stmt)).scalars().all())
        return items, int(total)

    async def list_by_ids(self, ids: list[int]) -> list[Group]:
        if not ids:
            return []
        stmt = select(Group).where(Group.id.in_(ids)).order_by(Group.id)
        return list((await self._s.execute(stmt)).scalars().all())

    async def get_leaders_bulk(self, group_ids: list[int]) -> dict[int, User]:
        """Return ``{group_id: leader_user}`` for the given groups."""
        if not group_ids:
            return {}
        stmt = (
            select(Group.id, User)
            .join(User, User.id == Group.leader_user_id)
            .where(Group.id.in_(group_ids))
        )
        out: dict[int, User] = {}
        for row in (await self._s.execute(stmt)).all():
            gid, user = int(row[0]), row[1]
            out[gid] = user
        return out

    async def member_counts_bulk(self, group_ids: list[int]) -> dict[int, int]:
        """Return ``{group_id: member_count}`` (members include the leader)."""
        if not group_ids:
            return {}
        stmt = (
            select(User.group_id, func.count(User.id))
            .where(User.group_id.in_(group_ids))
            .group_by(User.group_id)
        )
        out: dict[int, int] = {gid: 0 for gid in group_ids}
        for row in (await self._s.execute(stmt)).all():
            gid = int(row[0])
            out[gid] = int(row[1])
        return out

    # --- Writes ------------------------------------------------------------

    async def insert(self, *, name: str, leader_user_id: int | None) -> Group:
        """Insert a row in ``groups``. Caller must satisfy the FK invariant
        (the user must exist) and the unique constraint on ``leader_user_id``.
        ``leader_user_id`` may be ``None`` for orphan groups (FE-FIX #3).
        """
        group = Group(name=name, leader_user_id=leader_user_id)
        self._s.add(group)
        await self._s.flush()
        await self._s.refresh(group)
        return group

    async def rename(self, *, group_id: int, name: str) -> None:
        await self._s.execute(
            update(Group)
            .where(Group.id == group_id)
            .values(name=name, updated_at=datetime.now(UTC))
        )

    async def delete(self, group_id: int) -> None:
        await self._s.execute(delete(Group).where(Group.id == group_id))
