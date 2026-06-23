"""UserGroup model — additive M:N membership (ADR-0030).

Source of truth for mailbox/message visibility, Telegram-notification
addressing and team member counts. ``users.group_id`` is kept as the
"home"/primary team; every home membership is mirrored by a row here.

DDL contract: ``docs/03-data-model.md`` table ``user_groups`` + ADR-0030.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from shared.db import Base


class UserGroup(Base):
    __tablename__ = "user_groups"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    group_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("groups.id", ondelete="CASCADE"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        # A user cannot be in the same team twice; also serves the direct
        # "memberships of a user" lookup and guarantees idempotency of
        # ``POST /api/admin/users/{id}/groups`` (ADR-0030).
        UniqueConstraint("user_id", "group_id", name="uq_user_groups_user_group"),
        # Reverse lookup "members of a team" (member_counts / member list).
        Index("ix_user_groups_group_id", "group_id"),
    )
