"""Pydantic schemas for the admin module."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from backend.app.groups.schemas import UserBriefDTO

_USERNAME_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")
_VALID_ROLES_FOR_CREATE: frozenset[str] = frozenset({"group_leader", "group_member"})

# ADR-0038 §4: admin-set login-password strength — same rules as the self-set
# flow's stronger tier (12..128, at least one letter + one digit).
_PASSWORD_MIN_LEN = 12
_PASSWORD_MAX_LEN = 128
# Defensive cap on the number of additional teams accepted in one create call
# (ADR-0038 §5 / ADR-0030); the operator picks from a small set of teams.
_MAX_ADDITIONAL_GROUPS = 100


def _validate_password_strength(v: str) -> str:
    """Shared strength check for an admin-set login password (ADR-0038 §4)."""
    if not (_PASSWORD_MIN_LEN <= len(v) <= _PASSWORD_MAX_LEN):
        raise ValueError(f"password must be {_PASSWORD_MIN_LEN}..{_PASSWORD_MAX_LEN} characters")
    if not any(c.isalpha() for c in v):
        raise ValueError("password must contain at least one letter")
    if not any(c.isdigit() for c in v):
        raise ValueError("password must contain at least one digit")
    return v


class UserMailAccountSummary(BaseModel):
    id: int
    email: str
    display_name: str | None = None
    is_active: bool
    last_synced_at: datetime | None
    last_sync_error: str | None


class GroupBriefDTO(BaseModel):
    id: int
    name: str


class UserDTO(BaseModel):
    id: int
    username: str
    email: str | None
    display_name: str | None
    role: str
    group: GroupBriefDTO | None
    # ADR-0030: every team the user belongs to (home + additional). The home
    # team equals ``group`` (== ``users.group_id``); the rest are extra
    # memberships from ``user_groups`` and are the only ones the admin UI
    # offers to remove. Empty for super_admin. Order: ascending group_id.
    memberships: list[GroupBriefDTO]
    password_reset_required: bool
    # ADR-0038: ``password_encrypted IS NOT NULL`` — drives the /admin
    # "Password" column (true → mask + reveal button; false → "—"). The
    # password value itself is NEVER included in the listing — it is fetched
    # on demand via ``GET /api/admin/users/{id}/password``.
    has_password: bool
    lockout_until: datetime | None
    last_login_at: datetime | None
    created_at: datetime
    mail_accounts: list[UserMailAccountSummary]


class UsersListResponse(BaseModel):
    items: list[UserDTO]
    total: int
    page: int
    limit: int


def _trim_or_none(v: str | None) -> str | None:
    if v is None:
        return None
    s = v.strip()
    return s or None


class CreateUserRequest(BaseModel):
    """Body of ``POST /api/admin/users``.

    Note: the ``email`` column on ``users`` is preserved at the DB layer for
    backwards compatibility but is no longer accepted as input — newly
    created users always have ``email = NULL``. The field was removed from
    the public API at the user's request (UX feedback: it was never used).
    """

    username: str = Field(min_length=3, max_length=64)
    display_name: str | None = Field(default=None, max_length=100)
    role: str = Field(default="group_member")
    group_id: int | None = Field(default=None, ge=1)
    # ADR-0038 §3: optional admin-set login password. When present the backend
    # writes ``password_hash`` (argon2) AND ``password_encrypted`` (reversible)
    # and clears ``password_reset_required``. Empty/absent → the existing
    # self-set flow (``password_encrypted`` stays NULL → column "—").
    password: str | None = Field(default=None)
    # ADR-0038 §5 / ADR-0030: extra teams (beyond the home ``group_id``) to
    # add the user to in the same transaction. Only honoured for
    # ``role='group_member'``; ignored for leaders / super_admin.
    additional_group_ids: list[int] | None = Field(default=None)

    @field_validator("username")
    @classmethod
    def _normalise(cls, v: str) -> str:
        v = v.strip().lower()
        if not _USERNAME_RE.match(v):
            raise ValueError("username may only contain A-Z, 0-9, _ . -")
        return v

    @field_validator("display_name")
    @classmethod
    def _trim(cls, v: str | None) -> str | None:
        return _trim_or_none(v)

    @field_validator("role")
    @classmethod
    def _v_role(cls, v: str) -> str:
        if v not in _VALID_ROLES_FOR_CREATE:
            raise ValueError(
                "role must be 'group_leader' or 'group_member' " "(super_admin is seeded only)"
            )
        return v

    @field_validator("password", mode="before")
    @classmethod
    def _empty_password_to_none(cls, v: object) -> object:
        # Treat an empty string (common from a form field left blank) as
        # "not provided" → self-set flow. A whitespace-only or too-short value
        # is left to the strength check below to reject.
        if isinstance(v, str) and v == "":
            return None
        return v

    @field_validator("password")
    @classmethod
    def _v_password(cls, v: str | None) -> str | None:
        if v is None:
            return None
        return _validate_password_strength(v)

    @field_validator("additional_group_ids")
    @classmethod
    def _v_additional_group_ids(cls, v: list[int] | None) -> list[int] | None:
        if v is None:
            return None
        if len(v) > _MAX_ADDITIONAL_GROUPS:
            raise ValueError(f"at most {_MAX_ADDITIONAL_GROUPS} additional teams")
        for gid in v:
            if gid < 1:
                raise ValueError("additional_group_ids must be positive integers")
        return v

    @model_validator(mode="after")
    def _v_role_group_pair(self) -> CreateUserRequest:
        # Bug-fix #2: ``group_leader`` may now optionally be assigned to an
        # existing orphan group (``leader_user_id IS NULL``). The previous
        # rule ("group_id must be null for group_leader") was relaxed —
        # super-admin can either:
        #   - omit group_id → auto-create a new group (ADR-0019 §5), or
        #   - pass an existing group_id → service-layer validates that the
        #     group is leaderless and assigns the new user as leader.
        # FE-FIX round-4 #4: group_id is required for group_member at creation
        # time. Super-admin must pick a group; reassignment is via PATCH later.
        if self.role == "group_member" and self.group_id is None:
            raise ValueError("group_id is required when role='group_member'")
        return self


class CreateUserResponse(BaseModel):
    id: int
    username: str
    email: str | None
    display_name: str | None
    role: str
    group_id: int | None
    group: GroupBriefDTO | None
    # ADR-0038: whether a reversible login-password copy was stored (admin-set
    # ``password``). The value itself is never returned.
    has_password: bool = False


class ResetPasswordRequest(BaseModel):
    """Body of ``POST /api/admin/users/{id}/reset`` (ADR-0038 §3).

    ``password`` optional: present → admin-set flow (hash + reversible copy,
    ``password_reset_required=false``); empty/absent → the existing force
    self-set flow (``password_encrypted=NULL`` → column "—").
    """

    password: str | None = Field(default=None)

    @field_validator("password", mode="before")
    @classmethod
    def _empty_password_to_none(cls, v: object) -> object:
        if isinstance(v, str) and v == "":
            return None
        return v

    @field_validator("password")
    @classmethod
    def _v_password(cls, v: str | None) -> str | None:
        if v is None:
            return None
        return _validate_password_strength(v)


class UpdateUserRequest(BaseModel):
    display_name: str | None = Field(default=None, max_length=100)
    role: str | None = None
    group_id: int | None = Field(default=None, ge=1)

    # Sentinel: distinguish "not provided" from "set to null". The simplest
    # serialisation is a separate boolean for ``clear_display_name``.
    clear_display_name: bool = False
    clear_group_id: bool = False

    @field_validator("display_name")
    @classmethod
    def _trim_dn(cls, v: str | None) -> str | None:
        return _trim_or_none(v)

    @field_validator("role")
    @classmethod
    def _v_role(cls, v: str | None) -> str | None:
        if v is None:
            return None
        if v not in _VALID_ROLES_FOR_CREATE:
            raise ValueError(
                "role must be 'group_leader' or 'group_member' "
                "(super_admin cannot be granted via API)"
            )
        return v


class AddMembershipRequest(BaseModel):
    """Body of ``POST /api/admin/users/{user_id}/groups`` (ADR-0030)."""

    group_id: int = Field(ge=1)


class MembershipDTO(BaseModel):
    """Created membership returned by ``POST .../groups`` (ADR-0030)."""

    user_id: int
    group_id: int
    group: GroupBriefDTO
    created_at: datetime


class DeleteUserResponse(BaseModel):
    ok: bool
    deleted_attachments: int
    deleted_messages: int
    deleted_mail_accounts: int


class AuditEntryDTO(BaseModel):
    id: int
    actor_user_id: int
    action: str
    target_user_id: int | None
    target_username: str | None
    details: dict[str, Any] | None
    ip: str | None
    created_at: datetime


class AuditListResponse(BaseModel):
    items: list[AuditEntryDTO]
    total: int
    page: int
    limit: int


__all__ = [
    "AddMembershipRequest",
    "AuditEntryDTO",
    "AuditListResponse",
    "CreateUserRequest",
    "CreateUserResponse",
    "DeleteUserResponse",
    "GroupBriefDTO",
    "MembershipDTO",
    "ResetPasswordRequest",
    "UpdateUserRequest",
    "UserBriefDTO",
    "UserDTO",
    "UserMailAccountSummary",
    "UsersListResponse",
]
