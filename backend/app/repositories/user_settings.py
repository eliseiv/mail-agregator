"""Repository for ``users_settings`` (ADR-0022 §2.7).

Lazy storage: rows are inserted on first ``PATCH /api/me/settings``. Until
then, default behaviour is encoded by ``COALESCE(..., true)`` in the
recipients SQL (see :mod:`backend.app.repositories.telegram_notifications`).
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models import UserSettings


class UserSettingsRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def get(self, user_id: int) -> UserSettings | None:
        stmt = select(UserSettings).where(UserSettings.user_id == user_id)
        return (await self._s.execute(stmt)).scalar_one_or_none()

    async def get_tg_notifications_enabled(self, user_id: int) -> bool:
        """Resolve the effective ``tg_notifications_enabled`` value.

        Returns ``True`` (default) when no row exists for ``user_id`` —
        mirrors the ``COALESCE(..., true)`` semantics of the recipient SQL.
        """
        row = await self.get(user_id)
        if row is None:
            return True
        return bool(row.tg_notifications_enabled)

    async def upsert_tg_notifications_enabled(self, *, user_id: int, enabled: bool) -> UserSettings:
        """Insert-or-update the row, return the post-write state."""
        stmt = (
            pg_insert(UserSettings)
            .values(user_id=user_id, tg_notifications_enabled=enabled)
            .on_conflict_do_update(
                index_elements=[UserSettings.user_id],
                set_={"tg_notifications_enabled": enabled},
            )
            .returning(UserSettings)
        )
        return (await self._s.execute(stmt)).scalar_one()
