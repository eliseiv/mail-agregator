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

import re
from datetime import datetime

from pydantic import BaseModel, Field, field_validator, model_validator

from backend.app.send.schemas import _validate_addresses


class ExternalMailAccountDTO(BaseModel):
    """The mailbox a message belongs to — id/email/display_name ONLY.

    ADR-0029 §2/§Security: never expose ``encrypted_password`` / ``oauth_*`` /
    IMAP-UID / owner structures — only these three public fields.
    """

    id: int
    email: str
    display_name: str | None


class ExternalMailboxDTO(BaseModel):
    """One mailbox in ``GET /api/external/mailboxes`` (ADR-0037 §2 / ADR-0039 §4).

    ``id`` == ``mail_accounts.id`` == ``ExternalMessageDTO.mail_account.id`` (the
    CRM join key). ``is_active`` (``false`` = worker auto-disabled, ADR-0033) is
    exposed DELIBERATELY for the CRM (ADR-0037 §Security). NEVER any
    ``encrypted_password`` / ``oauth_*`` / ``smtp_*`` / ``imap_*`` / ``user_id`` /
    owner structures. ADR-0044 §4 (phase A1): ``group_id`` dropped (no teams).

    ADR-0039 §4: additionally carries the sync-status triplet
    (``last_synced_at`` / ``last_sync_error`` / ``consecutive_failures``) for the
    CRM status dot / diagnostics. Additive — no secrets exposed.
    """

    id: int
    email: str
    display_name: str | None
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

    ADR-0044 §4 (phase A1): the ``tags`` field went away with tags (ADR-0043 §4
    — the matching logic moved to the CRM).
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
    requires it **absent**, hence two models. ``ExternalMessageDTO`` is shared
    and unchanged.

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


# --- Generic send: POST /api/external/mailboxes/{id}/send ------------------
# ADR-0048 §1 (phase A2.1, ``docs/04-api-contracts.md`` §4f-send). Replaces the
# message-scoped reply above: messages live in the CRM, threading/defaults are
# built there, the aggregator is a thin SMTP executor.


# Defensive header bounds for the threading headers. The ADR fixes the ``subject``
# ceiling (998, RFC 5322 unfolded line) and says the aggregator writes
# ``In-Reply-To`` / ``References`` EXACTLY as passed (it never synthesises them),
# but sets no size for them: ``In-Reply-To`` carries a single ``Message-ID``
# (one unfolded line is the natural ceiling), while ``References`` is a
# whitespace-separated chain that legitimately grows with a long thread — bounded
# generously so a real chain is never rejected while an unbounded header can't be
# pushed through.
_MAX_IN_REPLY_TO_LEN = 998
_MAX_REFS_LEN = 65_536


# RFC 5322 §2.2.3 folding: a CRLF followed by at least one WSP (space/HTAB) is
# NOT part of the value — it is line-wrapping inserted by the sending MUA. Real
# ``References`` chains are folded almost always (they outgrow the 78-column
# soft limit after two or three hops), and the CRM forwards the stored header
# verbatim, folds included.
_FWS_RE = re.compile(r"(?:\r\n|\r|\n)[ \t]+")


def _clean_header(value: str | None) -> str | None:
    """Unfold FWS, then reject any remaining control chars in a header value.

    Two distinct things share the ASCII control range and must not be conflated:

    1. **Folding whitespace** (``CRLF`` + space/HTAB — RFC 5322 §2.2.3). It is
       transport-level line wrapping, semantically identical to a single space.
       A real ``References`` chain arrives folded, so REJECTING it would fail
       every reply in a thread longer than a couple of hops. It is therefore
       UNFOLDED here (``CRLF WSP`` → one space) — that is not the aggregator
       "synthesising" a header (ADR-0048 §1: written exactly as passed), it is
       the same header in its canonical unfolded form; the MIME builder re-folds
       it on the wire, and no ``Message-ID`` is lost or altered.
    2. **A bare CR/LF (no continuation WSP) or any other C0/C1/DEL char** — that
       is either a corrupt value or a header-injection attempt.
       :class:`email.message.EmailMessage` raises ``ValueError`` on it, so it is
       refused HERE as ``400 validation_error`` instead of blowing up as a 500
       inside the MIME builder. Such a value is NOT silently sanitised: a
       threading header that cannot be written as given is refused, not mangled.

    HTAB inside the (unfolded) value is legal WSP and is left alone.
    """
    if value is None:
        return None
    unfolded = _FWS_RE.sub(" ", value)
    if any(ch != "\t" and (ord(ch) < 0x20 or 0x7F <= ord(ch) <= 0x9F) for ch in unfolded):
        raise ValueError("header value must not contain control characters")
    return unfolded


class ExternalSendRequest(BaseModel):
    """Body of ``POST /api/external/mailboxes/{id}/send`` (ADR-0048 §1).

    Exactly what the CRM already sends (`CRM backend/app/services/mail_service.py`
    ``send_payload``): ``to`` / ``cc`` / ``subject`` / ``body_text`` plus the
    OPTIONAL threading headers ``in_reply_to`` / ``refs`` (present only when the
    CRM has them). The sender is the mailbox ``{id}`` from the path — never a
    body field; there is no ``bcc`` (surface reduction, as in the reply).

    Validation carried over from ADR-0035 (ADR-0048 §1, "не теряется"): every
    ``to``+``cc`` address is a valid e-mail; ``to``+``cc`` ≤ 100 in total;
    ``subject`` ≤ 998; ``body_text`` non-empty after ``strip`` and ≤ 1 MiB.

    ``to`` may be an empty list as long as ``cc`` carries a recipient (the CRM
    permits exactly that: `CRM mail_service.py::_prepare_reply` rejects only the
    case where BOTH are empty) — the "≥ 1 recipient" rule is therefore enforced
    over the UNION, not over ``to`` alone. A send with no recipient at all is a
    ``400 validation_error``, not an SMTP round-trip.
    """

    to: list[str] = Field(..., max_length=100)
    cc: list[str] | None = Field(default=None, max_length=100)
    subject: str | None = Field(default=None, max_length=998)  # RFC 5322 line
    body_text: str = Field(..., max_length=1_048_576)  # 1 MiB
    in_reply_to: str | None = Field(default=None, max_length=_MAX_IN_REPLY_TO_LEN)
    refs: str | None = Field(default=None, max_length=_MAX_REFS_LEN)

    @field_validator("to", "cc")
    @classmethod
    def _check_addresses(cls, v: list[str] | None) -> list[str] | None:
        # Same e-mail pattern as the reply / session send (``_validate_addresses``).
        if v is None:
            return None
        return _validate_addresses(v)

    @field_validator("body_text")
    @classmethod
    def _check_body_not_blank(cls, v: str) -> str:
        # Non-empty after strip; the RAW (un-stripped) value is sent so the
        # caller's intended formatting survives.
        if not v.strip():
            raise ValueError("body_text must not be empty")
        return v

    @field_validator("subject", "in_reply_to", "refs")
    @classmethod
    def _check_headers(cls, v: str | None) -> str | None:
        # Every value that ends up in a MIME header goes through the SAME guard:
        # folded (multi-line) values are unfolded, anything still carrying a bare
        # CR/LF / control char is refused as ``400 validation_error``.
        #
        # ``subject`` is included DELIBERATELY (it is a header like the other
        # two): inbound mail carries mis-folded / multi-line subjects (see
        # ``send/mime.py::_sanitize_header``) and the CRM builds ``"Re: " +
        # <stored subject>``, so without this the value reached
        # ``build_mime`` (``send/mime.py:62``), where ``EmailMessage`` raises
        # ``ValueError`` → an unhandled 500, while §4f-send mandates a ``400
        # validation_error`` for a malformed body. Rejecting (rather than
        # sanitising, as the forward path does) keeps the external contract
        # honest: the aggregator never silently rewrites a header the caller
        # gave it. The common real case — a *folded* subject — is not rejected,
        # it is unfolded and sent.
        return _clean_header(v)

    @model_validator(mode="after")
    def _check_recipients(self) -> ExternalSendRequest:
        total = len(self.to) + len(self.cc or [])
        if total == 0:
            raise ValueError("at least one recipient is required (to or cc)")
        # ADR-0048 §1: the 100-address ceiling is on the SUM of to+cc (the
        # per-field ``max_length=100`` above alone would allow 200).
        if total > 100:
            raise ValueError("too many recipients (to + cc must be <= 100)")
        return self


class ExternalSendResponse(BaseModel):
    """``200`` body of the generic send (ADR-0048 §1) — ``{smtp_message_id}``, nothing else.

    ``sent_id`` is DELIBERATELY absent (ADR-0048 §1): the aggregator no longer
    owns a durable record of what it sent (``sent_messages`` is under drop), so
    any id it returned would be a surrogate pointing at no row. The durable
    identifier is minted by the CRM from its own ``mail_sent_messages``.
    ``appended_to_sent`` stays internal (as in the reply, ADR-0035 §5).
    """

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

    Test fields + an optional ``display_name``. Owner is the ``crm-service``
    technical user (server-derived). ADR-0044 §4 (phase A1): ``group_id`` is
    dropped — mailbox-to-team ownership lives in the CRM only.
    """

    display_name: str | None = Field(default=None, max_length=100)


class ExternalMailboxUpdateRequest(BaseModel):
    """Body of ``PATCH /api/external/mailboxes/{id}`` (ADR-0039 §2).

    All fields optional. ``is_active`` uses presence-semantics via
    ``set_is_active`` (activate/deactivate). ADR-0044 §4 (phase A1):
    ``group_id`` / ``set_group_id`` went away with teams.
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
    is_active: bool | None = None
    set_is_active: bool = False

    @model_validator(mode="before")
    @classmethod
    def _presence(cls, data: object) -> object:
        """Infer ``set_is_active`` from JSON key presence."""
        if isinstance(data, dict):
            d = dict(data)
            if "is_active" in d and "set_is_active" not in d:
                d["set_is_active"] = True
            return d
        return data

    @property
    def has_account_fields(self) -> bool:
        """True when any credential / host / display_name change is set.

        Drives whether the PATCH delegates to ``MailAccountService.update`` (the
        credential/host path). A pure ``is_active`` toggle skips it and goes
        straight to ``set_active`` to avoid a needless credential rewrite.
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
        )


class ExternalMailboxTestResponse(BaseModel):
    """``200`` body of ``POST /api/external/mailboxes/test`` — both legs OK."""

    imap_ok: bool
    smtp_ok: bool


class ExternalMailboxSyncResponse(BaseModel):
    """``202`` body of ``POST /api/external/mailboxes/{id}/sync``."""

    queued: bool


# --- External Outlook OAuth (headless) (ADR-0045 §2, 04-api-contracts §4f-oauth) ---


class ExternalOAuthAuthorizeRequest(BaseModel):
    """Body of ``POST /api/external/mailboxes/oauth/authorize`` (ADR-0045 §2).

    ``crm_state`` is an OPAQUE CRM token (HMAC-signed blob) — the aggregator
    never parses or trusts its contents, only stores it in the Redis
    ``oauth_state:{state}`` payload alongside the PKCE verifier and echoes it
    back verbatim to the CRM ingest (§3). Bounded to 512 chars (ADR-0045 §1).
    """

    crm_state: str = Field(min_length=1, max_length=512)


class ExternalOAuthAuthorizeResponse(BaseModel):
    """``200`` body of ``POST /api/external/mailboxes/oauth/authorize`` (ADR-0045 §2).

    ``authorize_url`` is the Microsoft consent URL the CRM opens for the
    operator; ``state`` is the one-shot Redis-bound anti-fixation token echoed
    for tracking (the same value Microsoft returns to the callback).
    """

    authorize_url: str
    state: str
