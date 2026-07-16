"""Redis-backed rate limiting helpers (ADR-0009).

For our mixed form/JSON endpoints with custom keys (``username + IP``,
``setup-token``) we need imperative control that the slowapi decorator API
doesn't easily express. We therefore implement a tiny INCR/EX-based
fixed-window counter in Redis directly and call ``consume()`` from each
state-changing route.

ADR-0009 references slowapi; our implementation is a strict subset of what
slowapi would do (Redis backend, fixed-window) — so the contract is honoured
without depending on the slowapi runtime hook. The slowapi package is still
listed in ``pyproject.toml`` as it is referenced from
``docs/02-tech-stack.md`` and ADR-0009; switching to slowapi decorators is
out of scope for this rework. See backend rework round 2 reviewer note.

Limits actually consumed after ADR-0044: the session/UI routes (``POST /login``,
``/set-password``, the ``/api/mail-accounts*`` and ``/api/admin/*`` CRUD, the
session ``send``) went away with the UI in phases A1/A3, and with them their
``consume()`` calls. The connector's only surface is the machine API
(``docs/04-api-contracts.md`` §4d/§4f), keyed per IP:

- ``GET  /api/external/messages`` / ``/mailboxes``  120 / min (``LIMIT_EXTERNAL_API``)
- external WRITE: mailbox CRUD, OAuth, and the generic
  ``POST /mailboxes/{id}/send``                      60 / min (``LIMIT_EXTERNAL_WRITE``)

ADR-0048 §3 (phase A2.2): the legacy reply endpoint and its
``LIMIT_EXTERNAL_REPLY`` (30 / min) were removed once the CRM moved to the
generic send.

Each capacity is operator-tunable at consume-time from the matching
``*_RATE_LIMIT_PER_MINUTE`` setting. The pre-decommission ``Limit`` table was
pruned with the env sweep (phase G, TD-060) — only the two limits above remain.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import FastAPI, Request

from backend.app.exceptions import RateLimitedError
from shared.logging import get_logger
from shared.redis_client import get_redis

log = get_logger(__name__)


def client_ip(request: Request) -> str:
    """Best-effort client IP. Trusts ``X-Forwarded-For`` from the nginx reverse proxy."""
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    if request.client:
        return request.client.host
    return "0.0.0.0"


# ---------------------------------------------------------------------------
# Imperative limiter (used by routers)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Limit:
    """A named rate-limit policy: ``capacity`` per ``window_seconds``."""

    name: str
    capacity: int
    window_seconds: int


# The connector's only limited surface is the machine API (ADR-0044 §5). The
# pre-decommission table (login / set-password / account+admin CRUD / tags /
# Telegram / webhooks / forwarding) was pruned with the env sweep (phase G,
# TD-060) once phases A1/A3 removed the routes that consumed it.
#
# External PULL-API (ADR-0029 §1/§4): 120 req / 60 s per client IP. Consumed
# FIRST in the router — before any work with the API key — so a brute-force /
# flood of failed-auth attempts is rate-limited too (anti-bruteforce + DoS).
# ``capacity`` is overridden at consume-time from
# ``settings.EXTERNAL_API_RATE_LIMIT_PER_MINUTE`` so operators can tune the
# cap without a code redeploy. The static value here is the default fallback.
LIMIT_EXTERNAL_API = Limit(name="external_api", capacity=120, window_seconds=60)
# External write API — mailboxes CRUD + OAuth + generic send (ADR-0039 §1): 60
# req / 60 s per client IP — a SEPARATE budget from read (``LIMIT_EXTERNAL_API``
# 120/min) so a write flood cannot evict read and vice-versa. Consumed FIRST in
# the router — before any key work — like the read limit. ``capacity`` is
# overridden at consume-time from ``settings.EXTERNAL_WRITE_RATE_LIMIT_PER_MINUTE``
# (same override pattern as ``LIMIT_EXTERNAL_API``); the static value here is the
# default fallback.
LIMIT_EXTERNAL_WRITE = Limit(name="external_write", capacity=60, window_seconds=60)


async def consume(limit: Limit, key: str) -> None:
    """Atomically increment the counter for ``key`` and raise if over capacity.

    Implementation: ``INCR`` then ``EXPIRE`` if the value is 1 (first hit in
    a window). Pipelined; round-trip ~ 1 ms on local Redis.
    """
    if not key:
        # No key -> can't enforce; do nothing rather than fail-open silently.
        log.warning("rate_limit_no_key", limit_name=limit.name)
        return
    redis = get_redis()
    redis_key = f"rl:{limit.name}:{key}"
    async with redis.pipeline(transaction=False) as pipe:
        pipe.incr(redis_key)
        pipe.expire(redis_key, limit.window_seconds, nx=True)
        results = await pipe.execute()
    current = int(results[0])
    if current > limit.capacity:
        ttl = int(await redis.ttl(redis_key))
        raise RateLimitedError(
            "Rate limit exceeded.",
            retry_after=max(ttl, 1) if ttl > 0 else limit.window_seconds,
        )


def install_rate_limiter(_app: FastAPI) -> None:
    """No-op hook kept so the call-site in ``main.create_app`` stays stable.

    Previously installed a slowapi ``Limiter`` on ``app.state.limiter`` plus
    a ``RateLimitExceeded`` exception handler. Neither was ever consumed —
    we use the imperative ``consume()`` helper above, which raises our own
    :class:`RateLimitedError` (already handled by the domain-error handler
    in :mod:`backend.app.exceptions`). The slowapi facade was therefore
    dead infrastructure and is removed; if a future route prefers the
    decorator API we'll resurrect a minimal facade with an explicit
    cross-reference back to ADR-0009.
    """
