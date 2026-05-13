"""TelegramLink ORM (ADR-0022 §1).

DDL contract: ``docs/03-data-model.md`` table ``telegram_links``.

One row per linked Telegram user. PK is ``telegram_user_id`` so the
atomic upsert ``INSERT … ON CONFLICT (telegram_user_id) DO UPDATE`` can
re-bind a Telegram account to a different internal user without a
multi-statement transaction. ``user_id`` is UNIQUE → at most one
Telegram account per internal user.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, text
from sqlalchemy.orm import Mapped, mapped_column

from shared.db import Base


class TelegramLink(Base):
    __tablename__ = "telegram_links"

    telegram_user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    dead_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (Index("telegram_links_user_id_idx", "user_id"),)
