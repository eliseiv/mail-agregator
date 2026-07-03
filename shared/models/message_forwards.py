"""MessageForward ORM (ADR-0034 §1.2).

DDL contract: ``docs/03-data-model.md`` table ``message_forwards``.

Idempotency registry (fork of ``webhook_deliveries``): ``UNIQUE(message_id,
group_id)`` lets the dispatcher use ``INSERT ... ON CONFLICT DO NOTHING
RETURNING id`` to claim ownership of a (message, group) pair exactly once
BEFORE building/sending the forward. A finished attempt is stamped in-place:
success → ``sent_at = now()``; SMTP error → ``error`` (truncated, no host
detail). A row with ``error`` set is **not** retried (no recovery scan;
at-most-once after claim — ADR-0034 §3.6, TD-043).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from shared.db import Base


class MessageForward(Base):
    """Idempotency/claim registry: one row per forwarded (message, group)."""

    __tablename__ = "message_forwards"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    message_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("messages.id", ondelete="CASCADE"),
        nullable=False,
    )
    group_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("groups.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Snapshot of the destination address at send time (audit; the live
    # ``group_forwarding.forward_to`` may change later).
    forward_to: Mapped[str] = mapped_column(Text, nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Truncated (<=500 chars) SMTP error text, no host detail (ADR-0034 §3.6,
    # docs/06-security.md §1.14). A row with ``error`` set is never retried.
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        UniqueConstraint("message_id", "group_id", name="message_forwards_unique"),
        Index("message_forwards_message_id_idx", "message_id"),
        Index("message_forwards_group_id_idx", "group_id"),
    )
