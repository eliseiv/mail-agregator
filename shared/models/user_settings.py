"""UserSettings ORM (ADR-0022 §2.7).

DDL contract: ``docs/03-data-model.md`` table ``users_settings``.

Per-user preferences. On this iteration the only column is
``tg_notifications_enabled`` (opt-out for Telegram push-notifications,
default ``true``). Future preferences (language, list density, …) add
columns to this table rather than to ``users``.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, text
from sqlalchemy.orm import Mapped, mapped_column

from shared.db import Base


class UserSettings(Base):
    __tablename__ = "users_settings"

    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    tg_notifications_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
