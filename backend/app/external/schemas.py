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

from pydantic import BaseModel, Field, field_validator

from backend.app.send.schemas import _validate_addresses


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
