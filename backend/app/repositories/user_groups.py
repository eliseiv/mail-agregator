"""Repository for ``user_groups`` (ADR-0030 multi-group membership).

The join-table is the source of truth for visibility / notification
addressing / member counts. ``users.group_id`` remains the "home" team and
is always mirrored by a row here (backfilled in migration 019, kept in sync
by :class:`backend.app.admin.service.AdminService`).

Pure data access — business invariants (home membership can't be removed,
super_admin can't be added, leader can't be moved) live in the service
layer.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models import UserGroup


class UserGroupsRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    # --- Reads -------------------------------------------------------------

    async def list_group_ids_for_user(self, user_id: int) -> list[int]:
        """All team ids the user is a member of (home + additional)."""
        stmt = (
            select(UserGroup.group_id)
            .where(UserGroup.user_id == user_id)
            .order_by(UserGroup.group_id)
        )
        return [int(row[0]) for row in (await self._s.execute(stmt)).all()]

    async def list_group_ids_for_users(self, user_ids: list[int]) -> dict[int, list[int]]:
        """Bulk variant of :meth:`list_group_ids_for_user`.

        Returns ``{user_id: [group_id, ...]}`` for the supplied ids. Users
        without any membership row are simply absent from the mapping
        (callers default to an empty list). Read-only — used to render the
        per-user team chips on the admin page (ADR-0030); no business logic.
        """
        if not user_ids:
            return {}
        stmt = (
            select(UserGroup.user_id, UserGroup.group_id)
            .where(UserGroup.user_id.in_(user_ids))
            .order_by(UserGroup.user_id, UserGroup.group_id)
        )
        result: dict[int, list[int]] = {}
        for row in (await self._s.execute(stmt)).all():
            result.setdefault(int(row[0]), []).append(int(row[1]))
        return result

    async def exists(self, *, user_id: int, group_id: int) -> bool:
        stmt = select(UserGroup.id).where(
            UserGroup.user_id == user_id,
            UserGroup.group_id == group_id,
        )
        return (await self._s.execute(stmt)).first() is not None

    # --- Writes ------------------------------------------------------------

    async def add(self, *, user_id: int, group_id: int) -> bool:
        """Idempotent membership insert.

        Returns ``True`` when a new row was created, ``False`` when the
        membership already existed (UNIQUE conflict — no-op).
        """
        stmt = (
            pg_insert(UserGroup)
            .values(user_id=user_id, group_id=group_id)
            .on_conflict_do_nothing(index_elements=[UserGroup.user_id, UserGroup.group_id])
            .returning(UserGroup.id)
        )
        return (await self._s.execute(stmt)).first() is not None

    async def get_created_at(self, *, user_id: int, group_id: int) -> datetime | None:
        """``created_at`` of a membership row, or ``None`` if absent."""
        stmt = select(UserGroup.created_at).where(
            UserGroup.user_id == user_id,
            UserGroup.group_id == group_id,
        )
        row = (await self._s.execute(stmt)).first()
        return row[0] if row is not None else None

    async def remove(self, *, user_id: int, group_id: int) -> bool:
        """Delete a membership.

        Returns ``True`` when a row was deleted, ``False`` when none matched.
        """
        stmt = (
            delete(UserGroup)
            .where(UserGroup.user_id == user_id, UserGroup.group_id == group_id)
            .returning(UserGroup.id)
        )
        return (await self._s.execute(stmt)).first() is not None
