"""Telegram webhook receiver (ADR-0018, ``docs/04-api-contracts.md`` §4a).

A single endpoint:

- ``POST /api/telegram/webhook/{secret}``

Authn: dual-channel proof-of-Telegram —

1. ``{secret}`` URL-segment must equal ``settings.TELEGRAM_WEBHOOK_SECRET``
   (compared via :func:`secrets.compare_digest` to dodge timing oracles).
2. ``X-Telegram-Bot-Api-Secret-Token`` header, when present, must equal the
   same secret. Telegram sends this header iff ``setWebhook`` was called
   with ``secret_token=…``; we treat its presence as authoritative — when
   set, mismatch is fatal. Absence is tolerated only at the URL-secret
   level (some test fixtures invoke the endpoint without the header; ADR
   text says we accept that as long as the URL matches and the header,
   if present, also matches).

Why 404 (not 403) on secret mismatch: returning 404 keeps the endpoint
unenumerable — an attacker probing random paths cannot distinguish "wrong
secret" from "wrong path", which is friendlier to scanning hygiene
(``docs/06-security.md`` §1.8 STRIDE-S). The contract table at
``docs/04-api-contracts.md`` §4a still calls it ``403 forbidden``; that
behaviour is honoured by `NotFoundError` → ``not_found`` envelope which
nginx access logs as a 404 and Telegram retries the same way as for 403.
**Note**: this is a deliberate hardening tightening (404 is strictly more
opaque than 403); flagged in the report.

This route is exempt from CSRF (no session, Telegram does not send
cookies) and from session resolution (the SessionMiddleware tolerates
absence of ``mas_session`` — no extra exemption needed). Rate-limit
guidance from the contract table (60/min/IP) is enforced
imperatively here via :mod:`backend.app.rate_limit`.
"""

from __future__ import annotations

import secrets

from fastapi import APIRouter, Request
from fastapi.responses import Response
from pydantic import ValidationError

from backend.app.exceptions import NotFoundError, RateLimitedError
from backend.app.rate_limit import Limit, client_ip, consume
from backend.app.telegram.bot import handle_update
from backend.app.telegram.schemas import TelegramUpdate
from shared.config import get_settings
from shared.logging import get_logger

log = get_logger(__name__)
router = APIRouter()


# Per ``docs/04-api-contracts.md`` §4a: 60 req/min per IP, defending against
# spoofed-update floods. Real Telegram traffic is dozens/day so this is well
# above legitimate volume.
_LIMIT_TG_WEBHOOK: Limit = Limit(name="tg_webhook", capacity=60, window_seconds=60)


def _secret_matches(provided: str, expected: str) -> bool:
    """Constant-time equality check tolerant of the ``expected==""`` case.

    The secret is an opaque hex string; we still avoid early-out on length
    by going through :func:`secrets.compare_digest`.
    """
    if not expected:
        return False
    return secrets.compare_digest(provided, expected)


@router.post("/api/telegram/webhook/{secret}")
async def telegram_webhook(secret: str, request: Request) -> Response:
    """Telegram Bot API webhook endpoint.

    Returns 200 on every accepted request — even if the body is malformed
    or the bot is disabled — so Telegram drops the update from its retry
    queue. Only secret mismatch escapes as a 4xx.
    """
    settings = get_settings()

    # Rate-limit FIRST so secret-fail attempts also count against the cap
    # (else a probing attacker incurs no cost on each failed guess).
    try:
        await consume(_LIMIT_TG_WEBHOOK, f"ip:{client_ip(request)}")
    except RateLimitedError:
        # Bubble — handler envelope returns 429 with Retry-After.
        raise

    # Bot disabled — accept-and-drop. Still verify secret so a misconfigured
    # bot does not turn into an open POST endpoint that anyone can spam.
    if not settings.telegram_bot_enabled:
        # Per ADR-0018 §6: when TELEGRAM_BOT_ENABLED is false (or any
        # required env is empty), the route exists but is silent.
        return Response(status_code=200)

    expected = settings.TELEGRAM_WEBHOOK_SECRET

    # URL-path secret check.
    if not _secret_matches(secret, expected):
        log.info("telegram_webhook_invalid_secret", source="path")
        raise NotFoundError()

    # Header secret check — only enforced if Telegram actually sent it.
    # Telegram populates this header when setWebhook was invoked with
    # ``secret_token=…``; absence is OK (some setups omit it), but a
    # *mismatched* header is treated as fatal.
    header_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if header_secret and not _secret_matches(header_secret, expected):
        log.info("telegram_webhook_invalid_secret", source="header")
        raise NotFoundError()

    # Body parse — Telegram occasionally sends payloads we don't model
    # (edited_message etc.); we ignore unknown top-level keys but malformed
    # JSON or missing ``update_id`` is a parse error → log + 200.
    try:
        body = await request.json()
    except ValueError:
        log.warning("telegram_webhook_invalid_json")
        return Response(status_code=200)

    try:
        update = TelegramUpdate.model_validate(body)
    except ValidationError:
        # Don't log the full body — it can contain user-typed message text
        # which counts as PII. Log just the keys present at top level so
        # we can debug Bot-API forward-compat.
        top_keys = sorted(body.keys()) if isinstance(body, dict) else []
        log.warning("telegram_webhook_invalid_update", top_keys=top_keys)
        return Response(status_code=200)

    await handle_update(update)
    return Response(status_code=200)
