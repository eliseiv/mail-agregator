"""MIME builder for outgoing messages (text/plain; charset=utf-8).

``build_mime`` builds the classic text/plain compose/reply message.
``build_forward_mime`` (ADR-0034 §4) builds the richer forward message
(multipart: text + optional html alternative + attachments) used by the
leader-forwarding worker.
"""

# The forward "пересланное сообщение" block + skipped-attachment note contain
# Cyrillic literals that ruff's RUF001 flags as ambiguous — allow them
# file-wide (same approach as ``telegram/notify_format.py``).
# ruff: noqa: RUF001

from __future__ import annotations

import html
import uuid
from dataclasses import dataclass
from email.message import EmailMessage
from email.policy import SMTP
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from shared.config import get_settings

if TYPE_CHECKING:
    from shared.models import Message


def generate_message_id() -> str:
    """Generate ``Message-ID`` header value rooted at ``APP_BASE_URL``'s host."""
    settings = get_settings()
    host = urlparse(settings.APP_BASE_URL).hostname or "mail-aggregator.local"
    return f"<{uuid.uuid4()}@{host}>"


def build_mime(
    *,
    from_addr: str,
    to: list[str],
    cc: list[str] | None,
    bcc: list[str] | None,  # — intentionally absent from headers
    subject: str | None,
    body_text: str,
    in_reply_to_header: str | None,
    references_header: str | None,
    message_id: str,
) -> EmailMessage:
    """Build an :class:`EmailMessage` with stdlib's ``policy.SMTP``.

    BCC is not added to MIME headers (they would be visible to TO/CC
    recipients). Callers must pass BCC addresses to :func:`aiosmtplib.send`
    via the ``recipients=`` parameter, or rely on the SMTP client to use
    headers (we use ``recipients=`` explicitly).
    """
    msg = EmailMessage(policy=SMTP)
    msg["From"] = from_addr
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    if subject:
        msg["Subject"] = subject
    msg["Message-ID"] = message_id
    if in_reply_to_header:
        msg["In-Reply-To"] = in_reply_to_header
    if references_header:
        msg["References"] = references_header
    msg.set_content(body_text, subtype="plain", charset="utf-8")
    return msg


# ---------------------------------------------------------------------------
# Forward MIME (ADR-0034 §4)
# ---------------------------------------------------------------------------

_NO_SUBJECT = "(без темы)"
_FORWARD_SEPARATOR = "---------- Пересланное сообщение ----------"
_SKIPPED_ATTACHMENTS_PREFIX = "⚠️ Вложения не пересланы (слишком большие): "
_FORWARDED_BY_VALUE = "mail-aggregator"

# RFC 5322 §2.1.1 unfolded-line ceiling; a Subject longer than this is clamped.
_MAX_SUBJECT_LEN = 998
# Content-Disposition ``filename`` is far shorter in practice; clamp defensively.
_MAX_FILENAME_LEN = 255


def _sanitize_header(value: str, *, max_len: int = _MAX_SUBJECT_LEN) -> str:
    """Make ``value`` safe to place in a MIME header (ADR-0034 §4, CR/LF fix).

    ``email.message.EmailMessage`` raises ``ValueError`` ("Header values may
    not contain linefeed or carriage return characters") for a bare CR/LF in a
    header value — a header-injection guard. Inbound mail can carry a
    multi-line / malformed ``Subject`` (mis-folded, or crafted), which would
    otherwise abort the whole forward. Every value bound for a header is
    sanitised here:

    - any C0/C1 control char (incl. CR, LF, TAB) and DEL becomes a space;
    - runs of whitespace collapse to a single space and the ends are trimmed;
    - the result is clamped to ``max_len``.
    """
    cleaned = "".join(" " if (ord(ch) < 0x20 or 0x7F <= ord(ch) <= 0x9F) else ch for ch in value)
    cleaned = " ".join(cleaned.split())
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len].rstrip()
    return cleaned


@dataclass(frozen=True, slots=True)
class ForwardAttachmentPart:
    """One attachment resolved for a forward.

    ``data`` is the streamed bytes when the attachment is included; ``None``
    marks an attachment that was **skipped** (``skipped_too_large`` or the
    running ``FORWARD_MAX_TOTAL_BYTES`` budget was exceeded) — its filename is
    listed in the body note instead of attaching the payload. The caller
    (worker dispatcher) streams the bytes from MinIO and enforces the budget,
    so this builder stays pure/sync and easily testable.
    """

    filename: str
    content_type: str | None
    data: bytes | None


def _forward_prefix_rows(message: Message) -> list[tuple[str, str]]:
    """Build the ("label", "value") rows of the "forwarded message" block."""
    sender = message.from_name or message.from_addr or ""
    date_str = message.internal_date.isoformat() if message.internal_date else ""
    return [
        ("От", sender),
        ("Дата", date_str),
        ("Кому", message.to_addrs or ""),
        ("Тема", message.subject or _NO_SUBJECT),
    ]


def build_forward_mime(
    *,
    from_addr: str,
    forward_to: str,
    message: Message,
    attachment_parts: list[ForwardAttachmentPart],
    reply_to: str | None = None,
) -> EmailMessage:
    """Build the forward :class:`EmailMessage` (ADR-0034 §4).

    - ``Subject: Fwd: <original>`` (``Fwd: (без темы)`` when empty);
      ``From = from_addr``; ``To = forward_to``; a fresh ``Message-ID``;
      ``X-Forwarded-By: mail-aggregator`` (loop-guard stamp). When ``reply_to``
      is given a ``Reply-To`` header is added (relay branch, ADR-0034 §5: the
      forward is sent from the service relay ``from_addr`` while the leader's
      "Reply" must reach the *original* sender). ``from_addr``/``forward_to``/
      ``reply_to`` are sanitised defensively; ``Subject`` is sanitised to strip
      CR/LF from multi-line / malformed inbound subjects (else
      :class:`EmailMessage` raises ``ValueError`` on assignment).
    - Body: the plain-text part (and, when the source had one, an html
      alternative) is prefixed with the "forwarded message" block built from
      the stored :class:`Message` fields. Every html value is ``html.escape``-d.
    - Attachments: each :class:`ForwardAttachmentPart` with non-``None`` data
      is attached; skipped ones are listed in a body note. Total-size
      enforcement (``FORWARD_MAX_TOTAL_BYTES``) is the caller's job.
    """
    msg = EmailMessage(policy=SMTP)
    subject = _sanitize_header(message.subject or "")
    msg["Subject"] = f"Fwd: {subject}" if subject else f"Fwd: {_NO_SUBJECT}"
    msg["From"] = _sanitize_header(from_addr)
    msg["To"] = _sanitize_header(forward_to)
    if reply_to:
        msg["Reply-To"] = _sanitize_header(reply_to)
    msg["Message-ID"] = generate_message_id()
    msg["X-Forwarded-By"] = _FORWARDED_BY_VALUE

    rows = _forward_prefix_rows(message)
    skipped = [p.filename for p in attachment_parts if p.data is None]

    # --- text part -------------------------------------------------------
    text_lines = [_FORWARD_SEPARATOR]
    text_lines += [f"{label}: {value}" for label, value in rows]
    text_body = "\n".join(text_lines) + "\n\n" + (message.body_text or "")
    if skipped:
        text_body += "\n\n" + _SKIPPED_ATTACHMENTS_PREFIX + ", ".join(skipped)
    msg.set_content(text_body, subtype="plain", charset="utf-8")

    # --- html alternative (only when the source had an html body) --------
    if message.body_html:
        prefix_html = "".join(
            f"{html.escape(label)}: {html.escape(value)}<br>" for label, value in rows
        )
        html_body = (
            f"<div>{html.escape(_FORWARD_SEPARATOR)}<br>{prefix_html}</div><hr>"
            f"{message.body_html}"
        )
        if skipped:
            html_body += (
                "<p>" + html.escape(_SKIPPED_ATTACHMENTS_PREFIX + ", ".join(skipped)) + "</p>"
            )
        msg.add_alternative(html_body, subtype="html")

    # --- attachments -----------------------------------------------------
    for part in attachment_parts:
        if part.data is None:
            continue
        ct = part.content_type or "application/octet-stream"
        maintype, _, subtype = ct.partition("/")
        if not maintype or not subtype:
            maintype, subtype = "application", "octet-stream"
        msg.add_attachment(
            part.data,
            maintype=maintype,
            subtype=subtype,
            filename=_sanitize_header(part.filename, max_len=_MAX_FILENAME_LEN),
        )

    return msg
