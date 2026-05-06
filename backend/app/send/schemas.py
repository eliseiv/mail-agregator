"""Pydantic schemas for the send module."""

from __future__ import annotations

import re

from pydantic import BaseModel, Field, field_validator

# RFC 5322 is impractical; this is a pragmatic check matching the API spec
# (and what the bundled stdlib ``email`` module accepts).
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _validate_addresses(values: list[str]) -> list[str]:
    cleaned: list[str] = []
    for raw in values:
        addr = raw.strip()
        if not addr:
            continue
        if not _EMAIL_RE.match(addr):
            raise ValueError(f"invalid email address: {addr!r}")
        cleaned.append(addr)
    return cleaned


class SendMessageRequest(BaseModel):
    from_account_id: int = Field(..., ge=1)
    to: list[str] = Field(..., min_length=1, max_length=100)
    cc: list[str] | None = Field(default=None, max_length=100)
    bcc: list[str] | None = Field(default=None, max_length=100)
    subject: str | None = Field(default=None, max_length=998)
    body: str = Field(..., max_length=1_048_576)  # 1 MiB
    in_reply_to_message_id: int | None = Field(default=None, ge=1)

    @field_validator("to", "cc", "bcc")
    @classmethod
    def _check_addresses(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return None
        return _validate_addresses(v)


class SendMessageResponse(BaseModel):
    sent_id: int
    smtp_message_id: str
    appended_to_sent: bool
