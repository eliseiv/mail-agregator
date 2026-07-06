"""Pydantic schemas for the messages module."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from backend.app.accounts.schemas import OwnerBriefDTO
from backend.app.tags.schemas import TagBriefDTO


class AttachmentDTO(BaseModel):
    id: int
    filename: str
    content_type: str | None
    size_bytes: int
    skipped_too_large: bool


class MessageListItem(BaseModel):
    id: int
    mail_account_id: int
    mail_account_email: str
    # ADR-0020: nickname for the mail account; UI fallback to ``email``.
    mail_account_display_name: str | None = None
    # ADR-0019 §7: who actually owns the mailbox (for group-aware UI).
    owner: OwnerBriefDTO
    from_addr: str
    from_name: str | None
    subject: str | None
    internal_date: datetime
    is_read: bool
    has_attachments: bool
    # Gmail-style single-line body snippet (≤ ``PREVIEW_LEN`` chars,
    # whitespace collapsed). Empty string when the message has no body.
    preview: str = ""
    # ADR-0017: list of tags applied to this message (compact form).
    tags: list[TagBriefDTO] = Field(default_factory=list)


class MessageListResponse(BaseModel):
    items: list[MessageListItem]
    next_cursor: str | None = None


class MessageDetail(BaseModel):
    id: int
    mail_account_id: int
    mail_account_email: str
    mail_account_display_name: str | None = None
    owner: OwnerBriefDTO
    from_addr: str
    from_name: str | None
    to_addrs: str
    cc_addrs: str | None
    subject: str | None
    internal_date: datetime
    body_text: str
    # Round-12 bug B: sanitised HTML body (``shared.html_sanitize``).
    # ``None`` for legacy rows or text/plain-only emails; the template
    # falls back to ``body_text`` in that case.
    body_html: str | None = None
    body_truncated: bool
    body_present: bool
    in_reply_to: str | None
    is_read: bool
    attachments: list[AttachmentDTO]
    # ADR-0017: list of tags applied to this message (compact form).
    tags: list[TagBriefDTO] = Field(default_factory=list)


class MarkReadRequest(BaseModel):
    is_read: bool = Field(...)
