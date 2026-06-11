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

from pydantic import BaseModel


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
