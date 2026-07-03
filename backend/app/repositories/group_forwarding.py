"""Repository for ``group_forwarding`` (ADR-0034 §2, fork of ``WebhooksRepo``).

CRUD on the per-group forwarding configuration row. The repo does **not**
open its own transactions — the router wraps each mutating call in
``async with db.begin():`` so the audit write commits atomically with the
business row (symmetric to :class:`WebhooksRepo`).
"""

from __future__ import annotations

from sqlalchemy import delete, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models import GroupForwarding


class GroupForwardingRepo:
    """CRUD on ``group_forwarding``."""

    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    # --- Reads ------------------------------------------------------------

    async def get_by_id(self, forwarding_id: int) -> GroupForwarding | None:
        return await self._s.get(GroupForwarding, forwarding_id)

    async def get_by_group_id(self, group_id: int) -> GroupForwarding | None:
        stmt = text("SELECT id FROM group_forwarding WHERE group_id = :gid")
        result = await self._s.execute(stmt, {"gid": group_id})
        row = result.first()
        if row is None:
            return None
        # Re-hydrate via ORM ``get`` so SQLAlchemy state-management is
        # consistent with the rest of the codebase.
        return await self._s.get(GroupForwarding, int(row[0]))

    # --- Writes -----------------------------------------------------------

    async def insert(self, *, group_id: int, forward_to: str, is_active: bool) -> GroupForwarding:
        """INSERT a fresh row (no pre-reserved id needed — unlike ``webhooks``
        there is no secret whose AAD must bind to the row id)."""
        row = GroupForwarding(
            group_id=group_id,
            forward_to=forward_to,
            is_active=is_active,
        )
        self._s.add(row)
        await self._s.flush()
        await self._s.refresh(row)
        return row

    async def update_fields(self, forwarding_id: int, **fields: object) -> None:
        """Generic ``UPDATE group_forwarding SET ... WHERE id=:id``.

        The trigger ``trg_group_forwarding_updated_at`` keeps ``updated_at``
        in sync server-side, so callers don't pass it. ``created_at`` is never
        touched (it anchors the "don't flood history" filter — ADR-0034 §3.4).
        """
        if not fields:
            return
        await self._s.execute(
            update(GroupForwarding).where(GroupForwarding.id == forwarding_id).values(**fields)
        )

    async def delete(self, forwarding_id: int) -> None:
        """``DELETE FROM group_forwarding WHERE id=:id``.

        Does NOT touch ``message_forwards`` history (it FKs ``messages`` /
        ``groups``, not ``group_forwarding``); future forwarding simply stops.
        """
        await self._s.execute(delete(GroupForwarding).where(GroupForwarding.id == forwarding_id))
