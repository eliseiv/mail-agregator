"""Pydantic schemas for the external PULL-API (ADR-0029 §6).

These DTOs are **deliberately separate** from the UI ``MessageDetail`` /
``MessageService.get`` (module 10):

- The UI DTO applies render-time normalisation (``collapse_blank_lines_*``,
  ADR-0022 §2.10); the external contract must expose **raw stored** bodies.
- The external contract is a stable, independently-versioned wire format —
  fields evolve additively without touching the UI shape.

Field nullability mirrors the DB (``docs/03-data-model.md`` table ``messages``):
``subject`` / ``from_name`` / ``body_html`` / ``cc_addrs`` /
``mail_account.display_name`` are nullable; ``to_addrs`` is always a string
(``NOT NULL DEFAULT ''``). See ADR-0029 §2 + Edge cases.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator, model_validator

from backend.app.send.schemas import _validate_addresses
from backend.app.tags.schemas import (
    MatchMode,
    RuleType,
    _normalise_color,
)


class ExternalMailAccountDTO(BaseModel):
    """The mailbox a message belongs to — id/email/display_name ONLY.

    ADR-0029 §2/§Security: never expose ``encrypted_password`` / ``oauth_*`` /
    IMAP-UID / owner structures — only these three public fields.
    """

    id: int
    email: str
    display_name: str | None


class ExternalTagDTO(BaseModel):
    """A tag chip on a message (deduped by ``(name, color)`` upstream)."""

    id: int
    name: str
    color: str


class ExternalTeamDTO(BaseModel):
    """One team in ``GET /api/external/teams`` (ADR-0037 §1).

    A team == a ``groups`` row. Deliberately minimal: ``id``/``name`` ONLY —
    NO ``leader_user_id`` / ``created_at`` / ``members_count`` (unlike the heavy
    admin ``GET /api/admin/groups``). Team != tag (tags live in
    ``ExternalMessageDTO.tags``, ADR-0017 — untouched here).
    """

    id: int
    name: str


class ExternalTeamsResponse(BaseModel):
    """Envelope for ``GET /api/external/teams`` (ADR-0037 §1).

    ``teams`` is the flat, unpaginated list of all system teams
    (``GroupsRepo.list_all_groups()``, ``ORDER BY id``); empty system → ``[]``.
    """

    teams: list[ExternalTeamDTO]


class ExternalMailboxDTO(BaseModel):
    """One mailbox in ``GET /api/external/mailboxes`` (ADR-0037 §2 / ADR-0039 §4).

    ``id`` == ``mail_accounts.id`` == ``ExternalMessageDTO.mail_account.id`` (the
    CRM join key). ``group_id`` (mailbox→team mapping, ``null`` = personal) and
    ``is_active`` (``false`` = worker auto-disabled, ADR-0033) are exposed
    DELIBERATELY for the CRM (ADR-0037 §Security). NEVER any
    ``encrypted_password`` / ``oauth_*`` / ``smtp_*`` / ``imap_*`` / ``user_id`` /
    owner structures.

    ADR-0039 §4: additionally carries the sync-status triplet
    (``last_synced_at`` / ``last_sync_error`` / ``consecutive_failures``) for the
    CRM status dot / diagnostics. Additive — no secrets exposed.
    """

    id: int
    email: str
    display_name: str | None
    group_id: int | None
    is_active: bool
    last_synced_at: datetime | None
    last_sync_error: str | None
    consecutive_failures: int


class ExternalMailboxesResponse(BaseModel):
    """Envelope for ``GET /api/external/mailboxes`` (ADR-0037 §2).

    ``mailboxes`` is the canonical-deduped set (one ``MIN(id)`` mailbox per
    ``LOWER(email)`` — ADR-0029 §5), identical to the set whose messages
    ``GET /api/external/messages`` returns; no mailboxes → ``[]``.
    """

    mailboxes: list[ExternalMailboxDTO]


class ExternalMessageDTO(BaseModel):
    """One message in the external pull response (ADR-0029 §2).

    ``body_text`` / ``body_html`` are the **raw stored** values, WITHOUT the
    ``collapse_blank_lines_*`` render normalisation (ADR-0029 §3/§7). When
    ``body_present`` is false the email had no text/plain or text/html part —
    ``body_text=""`` and ``body_html=None`` (the fields are still present).
    """

    id: int
    subject: str | None
    internal_date: datetime
    from_addr: str
    from_name: str | None
    to_addrs: str
    cc_addrs: str | None
    mail_account: ExternalMailAccountDTO
    body_text: str
    body_html: str | None
    body_present: bool
    body_truncated: bool
    tags: list[ExternalTagDTO]


class ExternalMessagesResponse(BaseModel):
    """Page envelope for ``GET /api/external/messages`` (ADR-0029 §2).

    - ``next_since_id`` — ``id`` of the last item (``max(id)``, since the rows
      are ``ORDER BY id ASC``); the partner stores it as the new ``last_id``.
      On an empty page it equals the incoming ``since_id`` (cursor does not
      move).
    - ``has_more`` — ``len(messages) == limit`` heuristic ("maybe more"); a
      follow-up request with ``next_since_id`` confirms.
    """

    messages: list[ExternalMessageDTO]
    next_since_id: int
    has_more: bool


# ADR-0029 §6 refers to the page object as ``ExternalMessagesPage``; the
# canonical class here is ``ExternalMessagesResponse`` (used as the router's
# ``response_model``). This alias keeps the ADR name importable/consistent.
ExternalMessagesPage = ExternalMessagesResponse


class ExternalMessagesResponseDesc(BaseModel):
    """Backward / newest-first page envelope — ``order=desc`` (ADR-0036 §3).

    Kept as a **separate** model from :class:`ExternalMessagesResponse` (the
    ``asc`` forward page) so that each mode's cursor field is present ONLY in
    its own mode (ADR-0036 §3): the ``asc`` response carries ``next_since_id``
    and NEVER ``next_before_id``; this ``desc`` response carries
    ``next_before_id`` and NEVER ``next_since_id``. A single model with two
    optional cursors would emit the other mode's key as ``null`` — the ADR
    requires it **absent**, hence two models. ``ExternalMessageDTO`` /
    ``ExternalTagDTO`` are shared and unchanged.

    - ``messages`` — ``ExternalMessageDTO`` ordered ``id DESC`` (newest-first).
    - ``next_before_id`` — ``min(id)`` of the batch (= the last element's ``id``
      since the page is DESC); pass it back as ``before_id`` for the next
      (older) page. ``null`` when the batch is empty (no older messages left).
    - ``has_more`` — ``len(messages) == limit`` (same heuristic as forward).
    """

    messages: list[ExternalMessageDTO]
    next_before_id: int | None
    has_more: bool


# ADR-0036 migration step 5 names the backward page ``ExternalMessagesPageDesc``;
# alias keeps that ADR name importable next to ``ExternalMessagesPage`` (asc).
ExternalMessagesPageDesc = ExternalMessagesResponseDesc


# --- External reply-endpoint (ADR-0035 §2/§5) ------------------------------


class ExternalReplyRequest(BaseModel):
    """Body of ``POST /api/external/messages/{id}/reply`` (ADR-0035 §2).

    Deliberately narrow (ADR-0035 §Decision): NO ``from_account_id`` (the
    sender is the original message's mailbox, server-derived), NO ``bcc``
    (surface reduction), NO ``in_reply_to_message_id`` (threading is derived
    server-side from the path ``{id}``).

    Defaults that depend on the original message (``to`` → ``[from_addr]``,
    ``subject`` → ``"Re: " + subject``) are NOT resolved here — they are
    server-derived in :meth:`SendService.send_external_reply` (not user input,
    so they bypass this request validator, ADR-0035 §2).
    """

    to: list[str] | None = Field(default=None, max_length=100)
    cc: list[str] | None = Field(default=None, max_length=100)
    subject: str | None = Field(default=None, max_length=998)  # RFC 5322 line
    body: str = Field(..., max_length=1_048_576)  # 1 MiB — parity with send

    @field_validator("to", "cc")
    @classmethod
    def _check_addresses(cls, v: list[str] | None) -> list[str] | None:
        # Same e-mail pattern as the session ``send`` endpoint (ADR-0035 §2 —
        # reuse ``send/schemas.py:_EMAIL_RE`` via ``_validate_addresses``).
        if v is None:
            return None
        return _validate_addresses(v)

    @field_validator("body")
    @classmethod
    def _check_body_not_blank(cls, v: str) -> str:
        # Non-empty after strip (ADR-0035 §2 / Edge cases: whitespace-only body
        # → 400 validation_error, field=body). The raw (un-stripped) value is
        # sent so the partner's intended formatting is preserved.
        if not v.strip():
            raise ValueError("body must not be empty")
        return v


class ExternalReplyResponse(BaseModel):
    """200 body of the reply endpoint (ADR-0035 §5).

    A strict subset of the internal ``SendMessageResponse`` — ``appended_to_sent``
    is intentionally omitted (best-effort IMAP "Sent" append is an internal
    detail that does not affect the fact of sending; ADR-0035 §5 / Q-0035-2).
    """

    sent_id: int
    smtp_message_id: str


# --- External write API: mailboxes (ADR-0039 §2, 04-api-contracts §4f) ------


class ExternalMailboxTestRequest(BaseModel):
    """Body of ``POST /api/external/mailboxes/test`` (ADR-0039 §2).

    A full IMAP/SMTP credential set for a connectivity probe (no persistence).
    ``password`` / ``smtp_password`` are request-only (never echoed, redacted in
    logs). Mutual-exclusion of ``smtp_ssl`` / ``smtp_starttls`` + a basic e-mail
    shape are validated here so a bad payload surfaces as ``400`` at parse time
    (after auth/gate — ADR-0035 §3 order preserved by manual body parsing).
    """

    email: str = Field(min_length=3, max_length=254)
    imap_host: str = Field(min_length=1, max_length=253)
    imap_port: int = Field(ge=1, le=65535)
    imap_ssl: bool
    smtp_host: str = Field(min_length=1, max_length=253)
    smtp_port: int = Field(ge=1, le=65535)
    smtp_ssl: bool
    smtp_starttls: bool
    smtp_username: str | None = Field(default=None, max_length=254)
    password: str = Field(min_length=1, max_length=256)
    smtp_password: str | None = Field(default=None, max_length=256)

    @model_validator(mode="after")
    def _validate(self) -> ExternalMailboxTestRequest:
        if self.smtp_ssl and self.smtp_starttls:
            raise ValueError("smtp_ssl and smtp_starttls are mutually exclusive")
        if "@" not in self.email or "." not in self.email.split("@", 1)[1]:
            raise ValueError("email is not a valid address")
        local, _, domain = self.email.partition("@")
        if not local or domain.startswith(".") or domain.endswith(".") or ".." in domain:
            raise ValueError("email is not a valid address")
        return self


class ExternalMailboxCreateRequest(ExternalMailboxTestRequest):
    """Body of ``POST /api/external/mailboxes`` (ADR-0039 §2).

    Test fields + an optional ``display_name`` and ``group_id`` (validated to
    exist by the service, else ``404 group_not_found``; ``null`` = a box without
    a team). Owner is the ``crm-service`` technical user (server-derived).
    """

    display_name: str | None = Field(default=None, max_length=100)
    group_id: int | None = Field(default=None, ge=1)


class ExternalMailboxUpdateRequest(BaseModel):
    """Body of ``PATCH /api/external/mailboxes/{id}`` (ADR-0039 §2).

    All fields optional. ``group_id`` uses presence-semantics via
    ``set_group_id`` (the mere presence of the JSON key — even ``null`` — means
    "change the team", mirroring the internal ``MailAccountUpdateRequest``);
    ``is_active`` likewise via ``set_is_active`` (activate/deactivate).
    """

    email: str | None = Field(default=None, max_length=254)
    password: str | None = Field(default=None, max_length=256)
    display_name: str | None = Field(default=None, max_length=100)
    imap_host: str | None = Field(default=None, max_length=253)
    imap_port: int | None = Field(default=None, ge=1, le=65535)
    imap_ssl: bool | None = None
    smtp_host: str | None = Field(default=None, max_length=253)
    smtp_port: int | None = Field(default=None, ge=1, le=65535)
    smtp_ssl: bool | None = None
    smtp_starttls: bool | None = None
    smtp_username: str | None = Field(default=None, max_length=254)
    smtp_password: str | None = Field(default=None, max_length=256)
    group_id: int | None = Field(default=None, ge=1)
    set_group_id: bool = False
    is_active: bool | None = None
    set_is_active: bool = False

    @model_validator(mode="before")
    @classmethod
    def _presence(cls, data: object) -> object:
        """Infer ``set_group_id`` / ``set_is_active`` from JSON key presence."""
        if isinstance(data, dict):
            d = dict(data)
            if "group_id" in d and "set_group_id" not in d:
                d["set_group_id"] = True
            if "is_active" in d and "set_is_active" not in d:
                d["set_is_active"] = True
            return d
        return data

    @property
    def has_account_fields(self) -> bool:
        """True when any credential / host / display_name / group change is set.

        Drives whether the PATCH delegates to ``MailAccountService.update`` (the
        credential/host/transfer path). A pure ``is_active`` toggle skips it and
        goes straight to ``set_active`` to avoid a needless credential rewrite.
        """
        return (
            self.email is not None
            or self.password is not None
            or self.display_name is not None
            or self.imap_host is not None
            or self.imap_port is not None
            or self.imap_ssl is not None
            or self.smtp_host is not None
            or self.smtp_port is not None
            or self.smtp_ssl is not None
            or self.smtp_starttls is not None
            or self.smtp_username is not None
            or self.smtp_password is not None
            or self.set_group_id
        )


class ExternalMailboxTestResponse(BaseModel):
    """``200`` body of ``POST /api/external/mailboxes/test`` — both legs OK."""

    imap_ok: bool
    smtp_ok: bool


class ExternalMailboxSyncResponse(BaseModel):
    """``202`` body of ``POST /api/external/mailboxes/{id}/sync``."""

    queued: bool


# --- External write API: tags (ADR-0040 §4, 04-api-contracts §4f-tags) ------


class ExternalTagRuleDTO(BaseModel):
    """A persisted rule of a global tag (response side)."""

    id: int
    type: str
    pattern: str
    created_at: datetime


class ExternalTagFullDTO(BaseModel):
    """A global tag with its rules (ADR-0040 §4).

    Shape mirrors the internal ``TagDTO`` but is a deliberately separate wire
    type (ADR-0029 §6 stable-contract convention). Built from the service DTO.
    """

    id: int
    name: str
    color: str
    match_mode: str
    is_builtin: bool
    rules: list[ExternalTagRuleDTO]
    created_at: datetime
    updated_at: datetime


class ExternalTagsResponse(BaseModel):
    """Envelope for ``GET /api/external/tags``. Empty catalogue → ``[]``."""

    tags: list[ExternalTagFullDTO]


class ExternalTagCreateRequest(BaseModel):
    """Body of ``POST /api/external/tags`` (ADR-0040 §4).

    ``color`` is validated against the fixed palette (``^#[0-9A-Fa-f]{6}$`` +
    whitelist); ``match_mode`` defaults to ``any``.
    """

    name: str = Field(min_length=1, max_length=64)
    color: str
    # ``any`` (OR) by default — mirrors ``DEFAULT_MATCH_MODE`` / the internal
    # tag create request (a literal so the ``MatchMode`` type is preserved).
    match_mode: MatchMode = "any"

    @field_validator("name")
    @classmethod
    def _normalise_name(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("name cannot be empty after stripping")
        return stripped

    @field_validator("color")
    @classmethod
    def _check_color(cls, v: str) -> str:
        return _normalise_color(v)


class ExternalTagUpdateRequest(BaseModel):
    """Body of ``PATCH /api/external/tags/{id}`` — partial."""

    name: str | None = Field(default=None, min_length=1, max_length=64)
    color: str | None = None
    match_mode: MatchMode | None = None

    @field_validator("name")
    @classmethod
    def _normalise_name(cls, v: str | None) -> str | None:
        if v is None:
            return None
        stripped = v.strip()
        if not stripped:
            raise ValueError("name cannot be empty after stripping")
        return stripped

    @field_validator("color")
    @classmethod
    def _check_color(cls, v: str | None) -> str | None:
        if v is None:
            return None
        return _normalise_color(v)


class ExternalTagRuleCreateRequest(BaseModel):
    """Body of ``POST /api/external/tags/{id}/rules`` (ADR-0040 §4)."""

    type: RuleType
    pattern: str = Field(min_length=1, max_length=256)

    @field_validator("pattern")
    @classmethod
    def _strip_pattern(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("pattern cannot be empty after stripping")
        return stripped


class ExternalTagApplyResponse(BaseModel):
    """``200`` body of ``POST /api/external/tags/{id}/apply-to-existing``."""

    applied_count: int
