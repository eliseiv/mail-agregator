"""User model — the technical mailbox-owner table (ADR-0044 §1).

DDL contract: ``docs/03-data-model.md`` table ``users`` + ADR-0019.

ADR-0044 §3 (lock-step): ``group_id`` and the ``group`` relationship are
removed from the mapping BEFORE ``ALTER TABLE users DROP COLUMN group_id``
(phase E). After the decommission the table carries a single ``crm-service``
row (super_admin) — the owner of every mailbox (``mail_accounts.user_id`` NOT
NULL CASCADE).
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
    LargeBinary,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from shared.db import Base

# Roles allowed in ``users.role`` (mirrored by SQL CHECK ``ck_users_role``).
ROLE_SUPER_ADMIN = "super_admin"
ROLE_GROUP_LEADER = "group_leader"
ROLE_GROUP_MEMBER = "group_member"
ALL_ROLES: frozenset[str] = frozenset({ROLE_SUPER_ADMIN, ROLE_GROUP_LEADER, ROLE_GROUP_MEMBER})


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    email: Mapped[str | None] = mapped_column(Text, nullable=True)
    display_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # ADR-0038: reversible AES-256-GCM copy of the login password, kept ONLY
    # so a super_admin can reveal it in the /admin "Password" column. NULL =
    # no reversible copy (pre-ADR-0038 password unchanged, or reset in the
    # self-set flow) → the UI column shows "—". Never participates in login
    # verification (``password_hash`` remains the source of truth, ADR-0006);
    # never logged. Blob format: version_byte || iv(12B) || ct+tag
    # (``shared.crypto.encrypt_user_password``, AAD ``user_pw:{id}``).
    password_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    role: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default=text("'group_member'"),
    )
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
        # Defence-in-depth: case-insensitive uniqueness contract is enforced
        # in app code (lowercase before INSERT) AND by ``ck_users_username_lower``.
        CheckConstraint(
            "username = lower(username)",
            name="ck_users_username_lower",
        ),
        # Mirrors the SQL CHECK from the migration.
        CheckConstraint(
            "role IN ('super_admin', 'group_leader', 'group_member')",
            name="ck_users_role",
        ),
        CheckConstraint(
            "display_name IS NULL OR char_length(display_name) BETWEEN 1 AND 100",
            name="ck_users_display_name_length",
        ),
        # ADR-0044 §3 / phase E: the CHECK ``users_role_group_invariant`` and the
        # partial index ``ix_users_group_id_partial`` referenced the removed
        # ``group_id`` column — dropped from the mapping BEFORE the DDL (in the
        # DB they go away with the column itself, phase E).
        Index(
            "ix_users_role_super_admin_partial",
            "role",
            postgresql_where=text("role = 'super_admin'"),
        ),
    )

    # --- Convenience helpers ------------------------------------------------

    @property
    def is_super_admin(self) -> bool:
        """True iff the row's ``role`` equals ``super_admin``."""
        return self.role == ROLE_SUPER_ADMIN

    @property
    def is_group_leader(self) -> bool:
        return self.role == ROLE_GROUP_LEADER

    @property
    def is_group_member(self) -> bool:
        return self.role == ROLE_GROUP_MEMBER
