"""Repository for ``telegram_links`` (ADR-0022 §1 + ADR-0024).

Owns the atomic upsert by ``telegram_user_id`` (PK) — only way to safely
re-bind a Telegram account to a different internal user in a single
statement. Returns a ``replaced`` flag so the audit layer can distinguish
first-time link from re-binding.

ADR-0024 (Sprint A): ``UNIQUE(user_id)`` is dropped — a single internal
user may own several links. The ``user_id`` reads now return **lists**
(``list_by_user_id`` / ``list_active_by_user_id``); deletes split into
``delete_all_by_user_id`` (logout / reset) and ``delete_one`` (unlink a
specific TG). ``count_active_by_user_id`` backs the soft limit.

``dead_at`` semantics are unchanged: a row with non-NULL ``dead_at`` is
logically inactive for SSO / dispatch but kept for re-activation on the
next successful auth. ``mark_dead`` / ``mark_alive`` are per
``telegram_user_id`` (PK) and never touch sibling links of the same user.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import delete, func, select, update
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

    async def list_by_user_id(self, user_id: int) -> list[TelegramLink]:
        """All links for ``user_id`` (ADR-0024), live and dead, newest first.

        Replaces the pre-ADR-0024 ``get_by_user_id`` (which returned a single
        row). Used by the "my links" UI listing.
        """
        stmt = (
            select(TelegramLink)
            .where(TelegramLink.user_id == user_id)
            .order_by(TelegramLink.created_at.desc())
        )
        return list((await self._s.execute(stmt)).scalars().all())

    async def list_active_by_user_id(self, user_id: int) -> list[TelegramLink]:
        """Live links (``dead_at IS NULL``) for ``user_id`` (ADR-0024)."""
        stmt = (
            select(TelegramLink)
            .where(TelegramLink.user_id == user_id, TelegramLink.dead_at.is_(None))
            .order_by(TelegramLink.created_at.desc())
        )
        return list((await self._s.execute(stmt)).scalars().all())

    async def count_active_by_user_id(self, user_id: int) -> int:
        """Number of live links for ``user_id`` — backs the soft limit
        ``TG_MAX_LINKS_PER_USER`` (ADR-0024 §3)."""
        stmt = select(func.count()).where(
            TelegramLink.user_id == user_id, TelegramLink.dead_at.is_(None)
        )
        return int((await self._s.execute(stmt)).scalar_one())

    # --- Writes ------------------------------------------------------------

    async def upsert(self, *, telegram_user_id: int, user_id: int) -> tuple[TelegramLink, bool]:
        """Atomic upsert by ``telegram_user_id`` (PK).

        Returns ``(row, replaced)`` where ``replaced=True`` iff the row for
        this ``telegram_user_id`` already existed (the same TG account was
        re-bound to a possibly different internal user, or refreshed).

        ADR-0024: there is no longer a ``UNIQUE(user_id)`` constraint, so a
        second TG linking to the same internal user is a normal INSERT (not a
        collision). The conflict target is always the PK ``telegram_user_id``.
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

    async def delete_all_by_user_id(self, user_id: int) -> list[int]:
        """Delete **all** links for ``user_id`` (logout / reset) — ADR-0024 §5.

        Returns the list of deleted ``telegram_user_id`` values so the caller
        can record them in a single audit entry. Empty list if none existed.
        """
        stmt = (
            delete(TelegramLink)
            .where(TelegramLink.user_id == user_id)
            .returning(TelegramLink.telegram_user_id)
        )
        result = await self._s.execute(stmt)
        return [int(row[0]) for row in result.all()]

    async def delete_one(self, *, user_id: int, telegram_user_id: int) -> bool:
        """Unlink one specific TG (ADR-0024 §2). WHERE on **both** columns so
        a user cannot unlink someone else's TG. Returns ``True`` iff a row was
        deleted (idempotent: ``False`` when nothing matched)."""
        stmt = (
            delete(TelegramLink)
            .where(
                TelegramLink.user_id == user_id,
                TelegramLink.telegram_user_id == telegram_user_id,
            )
            .returning(TelegramLink.telegram_user_id)
        )
        result = await self._s.execute(stmt)
        return result.first() is not None

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
