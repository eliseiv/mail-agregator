"""Pydantic schemas for the admin module."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

_USERNAME_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")


class UserMailAccountSummary(BaseModel):
    id: int
    email: str
    is_active: bool
    last_synced_at: datetime | None
    last_sync_error: str | None


class UserDTO(BaseModel):
    id: int
    username: str
    email: str | None
    is_admin: bool
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


class CreateUserRequest(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    email: str | None = Field(default=None, max_length=254)

    @field_validator("username")
    @classmethod
    def _normalise(cls, v: str) -> str:
        v = v.strip().lower()
        if not _USERNAME_RE.match(v):
            raise ValueError("username may only contain A-Z, 0-9, _ . -")
        return v


class CreateUserResponse(BaseModel):
    id: int
    username: str
    email: str | None


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
