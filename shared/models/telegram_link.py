"""TelegramLink ORM (ADR-0022 §1 + ADR-0024).

DDL contract: ``docs/03-data-model.md`` table ``telegram_links``.

One row per linked Telegram user. PK is ``telegram_user_id`` so the
atomic upsert ``INSERT … ON CONFLICT (telegram_user_id) DO UPDATE`` can
re-bind a Telegram account to a different internal user without a
multi-statement transaction.

ADR-0024 (Sprint A): the "one internal user — one Telegram" invariant is
lifted. ``user_id`` is now a **1:N** FK (the ``UNIQUE(user_id)`` constraint
is dropped in migration ``20260527_017``) — one internal user may have
several active Telegram links (personal / work …), capped by the soft
limit ``TG_MAX_LINKS_PER_USER``. The reverse direction
(``telegram_user_id`` → ``user_id``) stays 1:1, so SSO resolution is still
unambiguous. ``user_id`` keeps a **non-unique** index
(``telegram_links_user_id_idx``) for logout / "my links" / recipient SQL.
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
        # ADR-0024: NO ``unique=True`` — one internal user may own several
        # Telegram links (1:N). A plain index (below) replaces the constraint.
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    dead_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (Index("telegram_links_user_id_idx", "user_id"),)
