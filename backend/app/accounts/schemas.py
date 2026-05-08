"""Pydantic schemas for the mail-accounts module.

Post-ADR-0019/ADR-0020:

- ``display_name`` is an optional 1..100-character label for the account.
- ``target_user_id`` controls the owner of a newly-created account
  (super_admin can create on any user; group_leader on any group member;
  group_member only on themselves — see ADR-0019 §8).
- The output DTO embeds ``owner`` (id, username, display_name) so the
  caller can render "whose mailbox is this" without an extra request.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, Field, field_validator, model_validator


class OwnerBriefDTO(BaseModel):
    """Mailbox owner brief — used in account list / message rows."""

    id: int
    username: str
    display_name: str | None = None


class MailAccountTestRequest(BaseModel):
    """Body of ``POST /api/mail-accounts/test``."""

    email: Annotated[str, Field(min_length=3, max_length=254)]
    password: Annotated[str, Field(min_length=1, max_length=256)]
    imap_host: Annotated[str, Field(min_length=1, max_length=253)]
    imap_port: Annotated[int, Field(ge=1, le=65535)] = 993
    imap_ssl: bool = True
    smtp_host: Annotated[str, Field(min_length=1, max_length=253)]
    smtp_port: Annotated[int, Field(ge=1, le=65535)] = 465
    smtp_ssl: bool = True
    smtp_starttls: bool = False
    smtp_username: str | None = Field(default=None, max_length=254)
    smtp_password: str | None = Field(default=None, max_length=256)

    @model_validator(mode="after")
    def _ssl_xor_starttls(self) -> MailAccountTestRequest:
        if self.smtp_ssl and self.smtp_starttls:
            raise ValueError("smtp_ssl and smtp_starttls are mutually exclusive")
        if "@" not in self.email or "." not in self.email.split("@", 1)[1]:
            raise ValueError("email is not a valid address")
        local, _, domain = self.email.partition("@")
        if not local or domain.startswith(".") or domain.endswith(".") or ".." in domain:
            raise ValueError("email is not a valid address")
        return self


class MailAccountCreateRequest(MailAccountTestRequest):
    """``POST /api/mail-accounts`` — same shape as test, plus optional
    ``display_name`` (ADR-0020) and ``target_user_id`` (ADR-0019 §8)."""

    display_name: str | None = Field(default=None, max_length=100)
    target_user_id: int | None = Field(default=None, ge=1)

    @field_validator("display_name")
    @classmethod
    def _trim_dn(cls, v: str | None) -> str | None:
        if v is None:
            return None
        s = v.strip()
        return s or None


class MailAccountUpdateRequest(BaseModel):
    """``PATCH /api/mail-accounts/{id}`` — partial."""

    email: str | None = Field(default=None, max_length=254)
    password: str | None = Field(default=None, max_length=256)
    display_name: str | None = Field(default=None, max_length=100)
    # Sentinel for "explicitly clear display_name to NULL" (form-encoded
    # empty value triggers this).
    clear_display_name: bool = False
    imap_host: str | None = Field(default=None, max_length=253)
    imap_port: int | None = Field(default=None, ge=1, le=65535)
    imap_ssl: bool | None = None
    smtp_host: str | None = Field(default=None, max_length=253)
    smtp_port: int | None = Field(default=None, ge=1, le=65535)
    smtp_ssl: bool | None = None
    smtp_starttls: bool | None = None
    smtp_username: str | None = Field(default=None, max_length=254)
    smtp_password: str | None = Field(default=None, max_length=256)

    @field_validator("display_name")
    @classmethod
    def _trim_dn(cls, v: str | None) -> str | None:
        if v is None:
            return None
        s = v.strip()
        return s or None


class MailAccountDTO(BaseModel):
    """Public (non-secret) representation of a mail account."""

    id: int
    user_id: int
    owner: OwnerBriefDTO
    email: str
    display_name: str | None = None
    imap_host: str
    imap_port: int
    imap_ssl: bool
    smtp_host: str
    smtp_port: int
    smtp_ssl: bool
    smtp_starttls: bool
    smtp_username: str | None
    is_active: bool
    last_synced_at: datetime | None
    last_sync_error: str | None
    consecutive_failures: int
    created_at: datetime


class TestResult(BaseModel):
    imap_ok: bool
    smtp_ok: bool
