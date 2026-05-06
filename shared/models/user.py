"""User model — service users (super-admin + ordinary users).

DDL contract: ``docs/03-data-model.md`` table ``users``.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from shared.db import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    email: Mapped[str | None] = mapped_column(Text, nullable=True)
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    password_reset_required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    lockout_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failed_login_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        # Partial index for fast super-admin lookup at seed time.
        Index(
            "ix_users_is_admin_partial",
            "is_admin",
            postgresql_where=text("is_admin = true"),
        ),
        # Defence-in-depth: even though app code lowercases before INSERT,
        # the DB-level CHECK guarantees no future code path can break the
        # case-insensitive uniqueness invariant. Migration:
        # ``20260505_002_lower_username_check.py``.
        CheckConstraint(
            "username = lower(username)",
            name="ck_users_username_lower",
        ),
    )
