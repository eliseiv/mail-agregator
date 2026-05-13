"""Repository for ``telegram_links`` (ADR-0022 §1).

Owns the atomic upsert by ``telegram_user_id`` (PK) — only way to safely
re-bind a Telegram account to a different internal user in a single
statement. Returns a ``replaced`` flag so the audit layer can distinguish
first-time link from re-binding.

Visibility-style helpers (``get_active_by_telegram_user_id``,
``get_by_user_id``) honour the ``dead_at`` marker: a row with non-NULL
``dead_at`` is considered logically inactive for SSO / dispatch but kept
in the table for re-activation on the next successful auth.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models import TelegramLink


class TelegramLinksRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    # --- Reads -------------------------------------------------------------

    async def get_by_telegram_user_id(self, telegram_user_id: int) -> TelegramLink | None:
        """Return the row keyed by Telegram User.id (regardless of liveness)."""
        return await self._s.get(TelegramLink, telegram_user_id)

    async def get_active_by_telegram_user_id(self, telegram_user_id: int) -> TelegramLink | None:
        """Return the row only if ``dead_at`` is NULL (used by SSO)."""
        stmt = select(TelegramLink).where(
            TelegramLink.telegram_user_id == telegram_user_id,
            TelegramLink.dead_at.is_(None),
        )
        return (await self._s.execute(stmt)).scalar_one_or_none()

    async def get_by_user_id(self, user_id: int) -> TelegramLink | None:
        stmt = select(TelegramLink).where(TelegramLink.user_id == user_id)
        return (await self._s.execute(stmt)).scalar_one_or_none()

    # --- Writes ------------------------------------------------------------

    async def upsert(self, *, telegram_user_id: int, user_id: int) -> tuple[TelegramLink, bool]:
        """Atomic upsert by ``telegram_user_id``.

        Returns ``(row, replaced)`` where ``replaced=True`` iff the conflict
        clause fired (the same Telegram account previously pointed at a
        different internal user, or the row existed and we refreshed it).

        Note: a UNIQUE clash on ``user_id`` (two different Telegram accounts
        attempting to link to the same internal user) is NOT handled here —
        the caller catches :class:`sqlalchemy.exc.IntegrityError` and writes
        a ``telegram_link_collision`` audit event.
        """
        existing = await self._s.get(TelegramLink, telegram_user_id)
        replaced = existing is not None
        stmt = (
            pg_insert(TelegramLink)
            .values(
                telegram_user_id=telegram_user_id,
                user_id=user_id,
            )
            .on_conflict_do_update(
                index_elements=[TelegramLink.telegram_user_id],
                set_={
                    "user_id": user_id,
                    "created_at": datetime.now(UTC),
                    "dead_at": None,
                },
            )
            .returning(TelegramLink)
        )
        row = (await self._s.execute(stmt)).scalar_one()
        return row, replaced

    async def delete_by_user_id(self, user_id: int) -> TelegramLink | None:
        """Delete the link for ``user_id`` (logout / reset).

        Returns the deleted row (with its ``telegram_user_id``) so the caller
        can include it in the audit record. Returns ``None`` if no link
        existed for the user.
        """
        existing = await self.get_by_user_id(user_id)
        if existing is None:
            return None
        await self._s.execute(delete(TelegramLink).where(TelegramLink.user_id == user_id))
        return existing

    async def mark_dead(self, telegram_user_id: int) -> None:
        """Mark the link as dead after a non-retriable Bot API failure
        (403 blocked, 400 chat_not_found). Idempotent — subsequent calls
        keep the earliest ``dead_at`` (PostgreSQL UPDATE accepts the same
        value harmlessly)."""
        await self._s.execute(
            update(TelegramLink)
            .where(
                TelegramLink.telegram_user_id == telegram_user_id,
                TelegramLink.dead_at.is_(None),
            )
            .values(dead_at=datetime.now(UTC))
        )

    async def mark_alive(self, telegram_user_id: int) -> None:
        """Clear the ``dead_at`` marker (used by SSO re-link upsert path —
        the upsert SET clause already does this, but kept here for explicit
        re-activation paths e.g. after admin intervention)."""
        await self._s.execute(
            update(TelegramLink)
            .where(TelegramLink.telegram_user_id == telegram_user_id)
            .values(dead_at=None)
        )
