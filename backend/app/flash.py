"""Flash message store backed by Redis.

Flash messages are short-lived notifications shown after a state-changing
form submission (e.g. "Account created", "Password reset").

Source of truth: ADR-0015 (no-JS fallback) + ``docs/05-modules.md`` sec. 3
(``flash:{session_id}`` Redis key).

Lifecycle:

1. After a successful (or failed) form-encoded request, the handler calls
   :func:`flash` to push an entry into Redis.
2. The browser follows a 303 ``See Other`` to a GET endpoint.
3. The HTML view calls :func:`consume_flashes` (via the templates render
   helper) which atomically reads-and-deletes the entries and passes them
   to the template context as ``flashes``.

Notes:

- The session id used as the key is the cookie value of ``mas_session`` (or
  ``mas_setup`` for the password-setup flow). For anonymous requests the
  flash helpers are no-ops — flash currently only matters for whitelist
  endpoints, all of which require an authenticated session.
- TTL is short (60 s) — long enough to survive redirect + render, but
  not long enough to leak into the next visit.
- Read-and-clear is atomic (Redis MULTI/EXEC pipeline with
  ``transaction=True``) so concurrent GETs cannot duplicate the same
  flash.
"""

from __future__ import annotations

import json
from typing import Literal

from starlette.requests import Request

from shared.logging import get_logger
from shared.redis_client import get_redis

log = get_logger(__name__)

FLASH_KEY_PREFIX = "flash:"
FLASH_TTL_SECONDS = 60

FlashCategory = Literal["success", "error", "info", "warning"]

_ALLOWED_CATEGORIES: frozenset[str] = frozenset({"success", "error", "info", "warning"})


def _session_id(request: Request) -> str | None:
    """Best session identifier for the request.

    Prefer the full user session cookie; fall back to the password-setup
    cookie. ``None`` if neither is present (anonymous request — flashes are
    silently dropped).
    """
    token: str | None = getattr(request.state, "session_token", None)
    if token:
        return token
    # Setup flow: only consulted for /set-password etc.
    setup_token = request.cookies.get("mas_setup")
    return setup_token or None


async def flash(request: Request, category: FlashCategory, text: str) -> None:
    """Push a flash entry into Redis for the current session.

    No-op when there is no session id (anonymous requests).
    """
    if category not in _ALLOWED_CATEGORIES:
        raise ValueError(f"unknown flash category: {category!r}")
    sid = _session_id(request)
    if sid is None:
        log.debug("flash_dropped_no_session", category=category)
        return
    payload = json.dumps({"category": category, "text": text}, separators=(",", ":"))
    redis = get_redis()
    key = FLASH_KEY_PREFIX + sid
    async with redis.pipeline(transaction=False) as pipe:
        pipe.rpush(key, payload)
        pipe.expire(key, FLASH_TTL_SECONDS)
        await pipe.execute()


async def consume_flashes(request: Request) -> list[dict[str, str]]:
    """Atomically read-and-delete the flash list for this session.

    Returns ``[]`` if the key is absent or the request has no session id.
    Each entry is a dict with ``category`` and ``text`` keys (matches the
    Jinja ``flash_messages()`` macro contract).
    """
    sid = _session_id(request)
    if sid is None:
        return []
    redis = get_redis()
    key = FLASH_KEY_PREFIX + sid
    # MULTI/EXEC so a parallel writer cannot have its entry deleted without
    # being read.
    async with redis.pipeline(transaction=True) as pipe:
        pipe.lrange(key, 0, -1)
        pipe.delete(key)
        results = await pipe.execute()
    raw_items = results[0] or []
    out: list[dict[str, str]] = []
    for raw in raw_items:
        try:
            item = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            log.warning("flash_corrupt_payload", session_id_prefix=sid[:8])
            continue
        if not isinstance(item, dict):
            continue
        category = str(item.get("category", "info"))
        text = str(item.get("text", ""))
        if category not in _ALLOWED_CATEGORIES:
            category = "info"
        out.append({"category": category, "text": text})
    return out
