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
    """Body of ``POST /api/mail-accounts/test``.

    Two modes (ADR-0025 §4c, docs/04-api-contracts §4c):

    - *Ad-hoc credential test* (account creation flow): the caller submits a
      full set of IMAP/SMTP credentials and no ``account_id``. This is the
      classic password-account path.
    - *Existing-account test* (``account_id`` set): the server resolves the
      stored account, ignores any submitted credentials and re-tests it using
      its persisted secrets. For ``auth_type='oauth_outlook'`` accounts this
      is the **only** way to test — it drives the XOAUTH2 path
      (refresh→access→connect); for password accounts it re-probes with the
      stored password.
    """

    account_id: int | None = Field(default=None, ge=1)
    email: str | None = Field(default=None, min_length=3, max_length=254)
    password: str | None = Field(default=None, min_length=1, max_length=256)
    imap_host: str | None = Field(default=None, min_length=1, max_length=253)
    imap_port: Annotated[int, Field(ge=1, le=65535)] = 993
    imap_ssl: bool = True
    smtp_host: str | None = Field(default=None, min_length=1, max_length=253)
    smtp_port: Annotated[int, Field(ge=1, le=65535)] = 465
    smtp_ssl: bool = True
    smtp_starttls: bool = False
    smtp_username: str | None = Field(default=None, max_length=254)
    smtp_password: str | None = Field(default=None, max_length=256)

    @model_validator(mode="after")
    def _validate(self) -> MailAccountTestRequest:
        if self.smtp_ssl and self.smtp_starttls:
            raise ValueError("smtp_ssl and smtp_starttls are mutually exclusive")
        # Existing-account mode: credentials are resolved server-side from the
        # stored row, so an ``account_id`` alone is a complete request.
        if self.account_id is not None:
            return self
        # Ad-hoc credential mode: a full credential set is mandatory.
        missing = [
            name
            for name, value in (
                ("email", self.email),
                ("password", self.password),
                ("imap_host", self.imap_host),
                ("smtp_host", self.smtp_host),
            )
            if not value
        ]
        if missing:
            raise ValueError(f"missing required fields: {', '.join(missing)}")
        assert self.email is not None
        if "@" not in self.email or "." not in self.email.split("@", 1)[1]:
            raise ValueError("email is not a valid address")
        local, _, domain = self.email.partition("@")
        if not local or domain.startswith(".") or domain.endswith(".") or ".." in domain:
            raise ValueError("email is not a valid address")
        return self


class MailAccountCreateRequest(BaseModel):
    """``POST /api/mail-accounts`` — full password-account credential set plus
    optional ``display_name`` (ADR-0020) and ``target_user_id`` (ADR-0019 §8).

    Distinct from :class:`MailAccountTestRequest` (which now allows the
    credential-less ``account_id`` test mode): account *creation* always
    requires the complete credential set, so every field is mandatory here.
    """

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
    display_name: str | None = Field(default=None, max_length=100)
    target_user_id: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _validate(self) -> MailAccountCreateRequest:
        if self.smtp_ssl and self.smtp_starttls:
            raise ValueError("smtp_ssl and smtp_starttls are mutually exclusive")
        if "@" not in self.email or "." not in self.email.split("@", 1)[1]:
            raise ValueError("email is not a valid address")
        local, _, domain = self.email.partition("@")
        if not local or domain.startswith(".") or domain.endswith(".") or ".." in domain:
            raise ValueError("email is not a valid address")
        return self

    @field_validator("display_name")
    @classmethod
    def _trim_dn(cls, v: str | None) -> str | None:
        if v is None:
            return None
        s = v.strip()
        return s or None

    def as_test_request(self) -> MailAccountTestRequest:
        """Project the create payload onto an ad-hoc credential test request."""
        return MailAccountTestRequest(
            email=self.email,
            password=self.password,
            imap_host=self.imap_host,
            imap_port=self.imap_port,
            imap_ssl=self.imap_ssl,
            smtp_host=self.smtp_host,
            smtp_port=self.smtp_port,
            smtp_ssl=self.smtp_ssl,
            smtp_starttls=self.smtp_starttls,
            smtp_username=self.smtp_username,
            smtp_password=self.smtp_password,
        )


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
    # ADR-0025: ``password`` | ``oauth_outlook``. ``oauth_needs_consent`` is
    # only meaningful for oauth accounts (UI shows a "reconnect" badge).
    auth_type: str = "password"
    oauth_needs_consent: bool = False
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
