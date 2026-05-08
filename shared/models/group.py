"""Group model — single-leader, many-member visibility scope.

DDL contract: ``docs/03-data-model.md`` table ``groups`` + ADR-0019.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from shared.db import Base

if TYPE_CHECKING:
    from shared.models.user import User


class Group(Base):
    __tablename__ = "groups"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    leader_user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    # `foreign_keys` is required because ``users.group_id`` also references
    # ``groups.id`` and SQLAlchemy cannot otherwise pick a single FK for
    # the ``leader`` relationship.
    leader: Mapped[User] = relationship(
        "User",
        foreign_keys=[leader_user_id],
        lazy="raise",
    )

    __table_args__ = (
        UniqueConstraint("leader_user_id", name="uq_groups_leader_user_id"),
        CheckConstraint(
            "char_length(name) BETWEEN 1 AND 100",
            name="ck_groups_name_length",
        ),
        Index("ix_groups_leader_user_id", "leader_user_id"),
    )
