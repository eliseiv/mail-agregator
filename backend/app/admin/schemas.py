"""Pydantic schemas for the admin module."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from backend.app.groups.schemas import UserBriefDTO

_USERNAME_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")
_VALID_ROLES_FOR_CREATE: frozenset[str] = frozenset({"group_leader", "group_member"})


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
    password_reset_required: bool
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

    @model_validator(mode="after")
    def _v_role_group_pair(self) -> CreateUserRequest:
        # ADR-0019 §5: new leader auto-creates the group → group_id must be null.
        if self.role == "group_leader" and self.group_id is not None:
            raise ValueError("group_id must be null when role='group_leader'")
        if self.role == "group_member" and self.group_id is None:
            raise ValueError("group_id is required for role='group_member'")
        return self


class CreateUserResponse(BaseModel):
    id: int
    username: str
    email: str | None
    display_name: str | None
    role: str
    group_id: int | None
    group: GroupBriefDTO | None


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
    "AuditEntryDTO",
    "AuditListResponse",
    "CreateUserRequest",
    "CreateUserResponse",
    "DeleteUserResponse",
    "GroupBriefDTO",
    "UpdateUserRequest",
    "UserBriefDTO",
    "UserDTO",
    "UserMailAccountSummary",
    "UsersListResponse",
]
