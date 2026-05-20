"""Pydantic schemas for outbound webhooks (ADR-0023 §2).

Three DTO shapes:

- :class:`WebhookCreateRequest` — body of ``POST /api/webhooks/me``.
- :class:`WebhookUpdateRequest` — body of ``PATCH /api/webhooks/me``
  (partial: any subset of ``{url, is_active}``).
- :class:`WebhookDTO`           — response for ``GET / PATCH`` — never
  includes the plaintext secret.
- :class:`WebhookCreatedDTO`    — response for ``POST`` and
  ``POST .../rotate-secret`` — extends :class:`WebhookDTO` with
  ``secret`` (plaintext, one-time-show).
- :class:`WebhookTestResult`    — response of the test endpoint.

The URL string is **not** validated here at the Pydantic layer beyond
length/charset; the service layer runs the full SSRF check via
:func:`shared.url_safety.validate_outbound_url`. We keep the API
boundary minimal — Pydantic just verifies that the field is a non-empty
string within the documented length range.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, Field

_URL_MAX_LEN: int = 2048


class WebhookCreateRequest(BaseModel):
    """``POST /api/webhooks/me {url: str}``."""

    url: Annotated[str, Field(min_length=9, max_length=_URL_MAX_LEN)]


class WebhookUpdateRequest(BaseModel):
    """``PATCH /api/webhooks/me`` — partial.

    All fields are optional; the service layer raises ``validation_error``
    if every field is ``None`` (no-op PATCH is rejected so we don't write
    an audit row for a non-change).
    """

    url: Annotated[str | None, Field(default=None, min_length=9, max_length=_URL_MAX_LEN)] = None
    is_active: bool | None = None


class WebhookDTO(BaseModel):
    """Response shape for ``GET`` / ``PATCH`` / ``DELETE`` — **no secret**."""

    id: int
    group_id: int
    url: str
    is_active: bool
    consecutive_failures: int
    dead_at: datetime | None
    last_fired_at: datetime | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime


class WebhookCreatedDTO(WebhookDTO):
    """``POST`` / ``rotate-secret`` response — secret is plaintext, shown
    exactly once. Receiver must store immediately."""

    secret: str


class WebhookSecretRevealDTO(BaseModel):
    """Minimal envelope returned by ``POST /api/webhooks/me/rotate-secret``
    for callers that only need the new secret (we still return the full
    :class:`WebhookCreatedDTO` so the UI can refresh its status block
    in-place without a follow-up GET)."""

    secret: str


class WebhookTestResult(BaseModel):
    """Response of ``POST /api/webhooks/me/test``.

    Even for receiver 5xx/timeout the API returns 200 with this body —
    it's a diagnostic operation, not a system failure. ``status`` is
    ``"ok"`` on 2xx, ``"http_error"`` on 4xx/5xx, ``"network"`` on
    transport-level failure.
    """

    status: str  # "ok" | "http_error" | "network" | "dns_failed"
    response_code: int | None
    response_excerpt: str | None
    duration_ms: int
    detail: str | None = None
