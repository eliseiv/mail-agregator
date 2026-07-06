"""User model — service users (super_admin / group_leader / group_member).

DDL contract: ``docs/03-data-model.md`` table ``users`` + ADR-0019.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from shared.db import Base

if TYPE_CHECKING:
    from shared.models.group import Group


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
    group_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("groups.id", ondelete="SET NULL", deferrable=True, initially="DEFERRED"),
        nullable=True,
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

    # Many-side relationship: a user belongs to at most one group (membership).
    # Disambiguated by ``foreign_keys`` because ``groups.leader_user_id`` also
    # links the two tables (``Group.leader``).
    group: Mapped[Group | None] = relationship(
        "Group",
        foreign_keys=[group_id],
        lazy="raise",
        primaryjoin="User.group_id == Group.id",
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
        # NOT VALID at migration time — see ``20260508_004_groups_and_roles.py``.
        # The ORM-level constraint mirrors the eventual ``VALIDATE`` semantics
        # so SQLAlchemy doesn't try to recreate it on autogen.
        CheckConstraint(
            "(role = 'super_admin'  AND group_id IS NULL) OR "
            "(role = 'group_leader' AND group_id IS NOT NULL) OR "
            "(role = 'group_member' AND group_id IS NOT NULL)",
            name="users_role_group_invariant",
        ),
        Index(
            "ix_users_role_super_admin_partial",
            "role",
            postgresql_where=text("role = 'super_admin'"),
        ),
        Index(
            "ix_users_group_id_partial",
            "group_id",
            postgresql_where=text("group_id IS NOT NULL"),
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
