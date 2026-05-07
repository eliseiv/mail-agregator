"""Tag, TagRule and MessageTag ORM models (ADR-0017).

DDL contract: ``docs/03-data-model.md`` tables ``tags``, ``tag_rules``,
``message_tags``.

Per-user isolation: ``tags.user_id`` FK with ON DELETE CASCADE so deleting
a user erases all of their tags + rules + links automatically.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    PrimaryKeyConstraint,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from shared.db import Base


class Tag(Base):
    __tablename__ = "tags"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    color: Mapped[str] = mapped_column(Text, nullable=False)
    is_builtin: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        UniqueConstraint("user_id", "name", name="uq_tags_user_name"),
        CheckConstraint(
            "char_length(name) BETWEEN 1 AND 64",
            name="ck_tags_name_length",
        ),
        CheckConstraint(
            r"color ~ '^#[0-9A-Fa-f]{6}$'",
            name="ck_tags_color_hex",
        ),
        Index("ix_tags_user_id", "user_id"),
    )


class TagRule(Base):
    __tablename__ = "tag_rules"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tag_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("tags.id", ondelete="CASCADE"),
        nullable=False,
    )
    type: Mapped[str] = mapped_column(Text, nullable=False)
    pattern: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        CheckConstraint(
            "type IN ('subject_contains','body_contains','sender_contains','sender_exact')",
            name="ck_tag_rules_type",
        ),
        CheckConstraint(
            "char_length(pattern) BETWEEN 1 AND 256",
            name="ck_tag_rules_pattern_length",
        ),
        Index("ix_tag_rules_tag_id", "tag_id"),
    )


class MessageTag(Base):
    __tablename__ = "message_tags"

    message_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("messages.id", ondelete="CASCADE"),
        nullable=False,
    )
    tag_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("tags.id", ondelete="CASCADE"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        PrimaryKeyConstraint("message_id", "tag_id", name="pk_message_tags"),
        Index("ix_message_tags_tag_message", "tag_id", "message_id"),
    )
