"""TelegramNotification ORM (ADR-0022 §2.3 + ADR-0024 §6).

DDL contract: ``docs/03-data-model.md`` table ``telegram_notifications``.

Idempotency registry for push-notifications. ADR-0024 changed the
idempotency key from ``(message_id, user_id)`` to
``(message_id, telegram_user_id)``: a user with several Telegram links
must receive the notification in **every** live chat, so the dedup key is
the concrete chat. ``telegram_user_id`` is the chat the notification was
delivered to (a snapshot of the chat_id at delivery time — **no FK** to
``telegram_links`` so the registry survives link delete / rebind, like
``telegram_message_id``). ``user_id`` is kept for audit ("what was
delivered to user X") and the recovery JOIN.
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
    # ADR-0024 §6: concrete chat the notification was delivered to. NOT NULL;
    # no FK (registry must outlive link delete/rebind). Legacy rows whose link
    # was gone at migration time carry the synthetic value 0 (TD-028).
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    __table_args__ = (
        # ADR-0024 §6: dedup per (message_id, telegram_user_id) — one row per
        # chat, not per user. Replaces the old (message_id, user_id) key.
        UniqueConstraint(
            "message_id", "telegram_user_id", name="telegram_notifications_msg_chat_uq"
        ),
        Index("telegram_notifications_message_id_idx", "message_id"),
        Index("telegram_notifications_user_id_idx", "user_id"),
    )
