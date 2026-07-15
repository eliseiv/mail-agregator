"""Pydantic schemas for the send module.

ADR-0044 §4 (phase A3): ``SendMessageRequest`` (the body of the session
``POST /api/messages/send``) went away with the HTML UI. ADR-0048 §3 (phase
A2.2): ``SendMessageResponse`` (the ``{sent_id, ...}`` reply of the removed
``_send_core`` / ``send_external_reply``) went away with the legacy reply path.
What remains is the shared address validation (reused by ``external/schemas.py``).
"""

from __future__ import annotations

import re

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
