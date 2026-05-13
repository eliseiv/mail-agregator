"""TelegramNotification ORM (ADR-0022 §2.3).

DDL contract: ``docs/03-data-model.md`` table ``telegram_notifications``.

Idempotency registry for push-notifications: one row per delivered
(or attempted) ``(message_id, user_id)`` pair. UNIQUE constraint
guarantees we never double-deliver the same notification.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from shared.db import Base


class TelegramNotification(Base):
    __tablename__ = "telegram_notifications"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    message_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("messages.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    __table_args__ = (
        UniqueConstraint("message_id", "user_id", name="telegram_notifications_unique"),
        Index("telegram_notifications_message_id_idx", "message_id"),
        Index("telegram_notifications_user_id_idx", "user_id"),
    )
