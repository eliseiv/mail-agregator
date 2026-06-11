"""External PULL-API router (ADR-0029 §1/§4; ``docs/04-api-contracts.md`` §4d).

``GET /api/external/messages`` — a B2B partner incrementally pulls ALL system
messages with a keyset cursor over ``messages.id``.

Auth flow (strict order, ADR-0029 §4):

1. ``consume(LIMIT_EXTERNAL_API, ip)`` FIRST — anti-flood before any work with
   the key (a failed-auth flood is rate-limited too). 429 on exhaustion.
2. extract the key: ``X-API-Key`` (priority) or ``Authorization: Bearer <key>``.
3. feature off (``EXTERNAL_API_KEY`` empty) → 401 ``not_authenticated`` —
   unenumerable, the config is not disclosed.
4. missing / wrong key → 401 ``not_authenticated`` (constant-time compare).
5. FastAPI validates the query (``since_id``/``limit``) → 400/422.
6. delegate to :class:`ExternalMessagesService`.

This route is CSRF-exempt (``backend/app/csrf.py``) and needs no cookie session.
The key is NEVER logged (redacted: ``EXTERNAL_API_KEY`` / ``X-API-Key`` /
``Authorization``).
"""

from __future__ import annotations

import secrets

from fastapi import APIRouter, Query, Request

from backend.app.deps import DbSession
from backend.app.exceptions import NotAuthenticatedError
from backend.app.external.schemas import ExternalMessagesResponse
from backend.app.external.service import ExternalMessagesService
from backend.app.rate_limit import LIMIT_EXTERNAL_API, Limit, client_ip, consume
from shared.config import get_settings
from shared.logging import get_logger

log = get_logger(__name__)
router = APIRouter(prefix="/api/external")

# ADR-0029 §1: hard query bounds.
_DEFAULT_LIMIT: int = 50
_MAX_LIMIT: int = 200


def _bearer(authorization: str | None) -> str | None:
    """Extract ``<token>`` from an ``Authorization: Bearer <token>`` header.

    Returns ``None`` for a missing/malformed/non-Bearer header. Case-insensitive
    on the ``Bearer`` scheme; an empty token yields ``None``.
    """
    if not authorization:
        return None
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    token = parts[1].strip()
    return token or None


def _api_key_matches(provided: str, expected: str) -> bool:
    """Constant-time key comparison (ADR-0029 §Security).

    An empty ``expected`` (feature off) always returns ``False`` without an
    early-out on length — same pattern as the Telegram webhook secret check.
    """
    if not expected:
        return False
    return secrets.compare_digest(provided, expected)


@router.get("/messages", response_model=ExternalMessagesResponse)
async def list_external_messages(
    request: Request,
    db: DbSession,
    since_id: int = Query(default=0, ge=0),
    limit: int = Query(default=_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT),
) -> ExternalMessagesResponse:
    """Incrementally pull system messages (ADR-0029). See module docstring."""
    ip = client_ip(request)
    settings = get_settings()

    # 1. Rate-limit FIRST — before any key work (anti-bruteforce + DoS). 429.
    #    Capacity is operator-tunable at consume-time from
    #    ``settings.EXTERNAL_API_RATE_LIMIT_PER_MINUTE`` (same override pattern
    #    as ``WEBHOOK_TEST_LIMIT`` / ``TG_SEND_PER_CHAT_PER_MINUTE``); the static
    #    ``LIMIT_EXTERNAL_API`` only supplies the name + fixed 60 s window.
    runtime_limit = Limit(
        name=LIMIT_EXTERNAL_API.name,
        capacity=settings.EXTERNAL_API_RATE_LIMIT_PER_MINUTE,
        window_seconds=LIMIT_EXTERNAL_API.window_seconds,
    )
    await consume(runtime_limit, f"ip:{ip}")

    # 2. Extract key: X-API-Key takes priority, else Authorization: Bearer.
    key = request.headers.get("X-API-Key") or _bearer(request.headers.get("Authorization"))

    # 3. Feature off — opaque 401 (do NOT reveal that the feature is disabled).
    if not settings.external_api_enabled:
        log.info("external_pull_unauthorized", client_ip=ip)
        raise NotAuthenticatedError()

    # 4. Missing / wrong key — same opaque 401 (constant-time compare; a None
    #    key short-circuits to 401 without a compare — see ADR-0029 §4).
    if key is None or not _api_key_matches(key, settings.EXTERNAL_API_KEY):
        log.info("external_pull_unauthorized", client_ip=ip)
        raise NotAuthenticatedError()

    # 5. Query already validated by FastAPI (since_id>=0, 1<=limit<=200).
    # 6. Delegate to the data service.
    result = await ExternalMessagesService(db).list_messages(since_id=since_id, limit=limit)

    log.info(
        "external_pull",
        client_ip=ip,
        since_id=since_id,
        limit=limit,
        returned=len(result.messages),
    )
    return result
