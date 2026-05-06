"""Pydantic schemas for the messages module."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


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
    from_addr: str
    from_name: str | None
    subject: str | None
    internal_date: datetime
    is_read: bool
    has_attachments: bool


class MessageListResponse(BaseModel):
    items: list[MessageListItem]
    next_cursor: str | None = None


class MessageDetail(BaseModel):
    id: int
    mail_account_id: int
    mail_account_email: str
    from_addr: str
    from_name: str | None
    to_addrs: str
    cc_addrs: str | None
    subject: str | None
    internal_date: datetime
    body_text: str
    body_truncated: bool
    body_present: bool
    in_reply_to: str | None
    is_read: bool
    attachments: list[AttachmentDTO]


class MarkReadRequest(BaseModel):
    is_read: bool = Field(...)
