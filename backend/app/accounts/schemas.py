"""Pydantic schemas for the mail-accounts module."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, Field, model_validator


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
        # Sanity: deliberately narrow check (not full RFC 5322). Using
        # ``EmailStr`` would pull in ``email-validator`` as a hard dependency
        # which is not currently in ``pyproject.toml``; the IMAP/SMTP server
        # performs the authoritative validation when the credentials are
        # tested. We still reject obviously-broken addresses here so we fail
        # before reaching the network round-trip.
        if "@" not in self.email or "." not in self.email.split("@", 1)[1]:
            raise ValueError("email is not a valid address")
        # Reject empty local-part / domain-part / trailing dot patterns that
        # the lightweight check above misses (e.g. ``a@.com``, ``@host.com``).
        local, _, domain = self.email.partition("@")
        if not local or domain.startswith(".") or domain.endswith(".") or ".." in domain:
            raise ValueError("email is not a valid address")
        return self


class MailAccountCreateRequest(MailAccountTestRequest):
    """``POST /api/mail-accounts`` — same shape as test."""


class MailAccountUpdateRequest(BaseModel):
    """``PATCH /api/mail-accounts/{id}`` — partial."""

    email: str | None = Field(default=None, max_length=254)
    password: str | None = Field(default=None, max_length=256)
    imap_host: str | None = Field(default=None, max_length=253)
    imap_port: int | None = Field(default=None, ge=1, le=65535)
    imap_ssl: bool | None = None
    smtp_host: str | None = Field(default=None, max_length=253)
    smtp_port: int | None = Field(default=None, ge=1, le=65535)
    smtp_ssl: bool | None = None
    smtp_starttls: bool | None = None
    smtp_username: str | None = Field(default=None, max_length=254)
    smtp_password: str | None = Field(default=None, max_length=256)


class MailAccountDTO(BaseModel):
    """Public (non-secret) representation of a mail account."""

    id: int
    email: str
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
