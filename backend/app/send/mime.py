"""MIME builder for outgoing messages (text/plain; charset=utf-8).

``build_mime`` builds the classic text/plain compose/reply message — the only
builder left after ADR-0044 (see the note at the foot of this module).
"""

from __future__ import annotations

import uuid
from email.message import EmailMessage
from email.policy import SMTP
from urllib.parse import urlparse

from shared.config import get_settings


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


# ADR-0044 + TD-060: the forward MIME builder (``build_forward_mime`` /
# ``ForwardAttachmentPart``, ADR-0034 §4) went away with the forwarding
# subsystem — the worker dispatcher that called it no longer exists. The
# external send path validates (rather than sanitises) inbound header values;
# see ``external/schemas.py::_clean_header``.
