"""Repository for ``message_forwards`` (ADR-0034 §1.2, fork of
``WebhookDeliveriesRepo``).

Claim / finalise the per-(message, group) idempotency rows. Neither this
repo nor :class:`GroupForwardingRepo` opens its own transaction — the worker
job wraps the work in ``async with make_session() as s, s.begin():``.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models import MessageForward

# ``message_forwards.error`` is clamped to this many characters (ADR-0034
# §1.2 / docs/06-security.md §1.14 — no host detail, truncated to 500 bytes).
_ERROR_MAX_CHARS = 500


class MessageForwardsRepo:
    """Claim / finalise the per-event idempotency rows."""

    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def try_reserve(self, *, message_id: int, group_id: int, forward_to: str) -> int | None:
        """Claim ``(message_id, group_id)`` BEFORE building/sending the forward.

        ``INSERT ... ON CONFLICT (message_id, group_id) DO NOTHING RETURNING
        id``. Returns the new row id, or ``None`` if a row already existed
        (idempotency — the message was already forwarded for this team, or
        another claim is in flight). Exactly-once even on queue duplicates /
        re-enqueue / worker restart.
        """
        stmt = (
            pg_insert(MessageForward)
            .values(message_id=message_id, group_id=group_id, forward_to=forward_to)
            .on_conflict_do_nothing(
                index_elements=[MessageForward.message_id, MessageForward.group_id]
            )
            .returning(MessageForward.id)
        )
        result = await self._s.execute(stmt)
        row = result.first()
        if row is None:
            return None
        return int(row[0])

    async def mark_sent(self, forward_id: int) -> None:
        """Finalise a claim after a successful SMTP send: ``sent_at = now()``."""
        await self._s.execute(
            update(MessageForward)
            .where(MessageForward.id == forward_id)
            .values(sent_at=datetime.now(UTC))
        )

    async def mark_error(self, forward_id: int, error: str) -> None:
        """Record an SMTP/build failure on the claim row (NOT retried).

        ``error`` is clamped to 500 chars and stripped of CR/LF so a
        multi-line SMTP diagnostic stays a single, bounded column value
        (no host detail — the caller passes an already-safe string).
        """
        error_clamped = (error or "").replace("\r", " ").replace("\n", " ")[:_ERROR_MAX_CHARS]
        await self._s.execute(
            update(MessageForward)
            .where(MessageForward.id == forward_id)
            .values(error=error_clamped)
        )
