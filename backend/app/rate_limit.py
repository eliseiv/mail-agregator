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

Limits (``docs/04-api-contracts.md`` table sec. 8):

- ``POST /login``                     30 / 15 min (per IP) — step-1 of two-step
                                                 login (ADR-0016); username only.
- ``POST /login/password``            5  / 15 min (username + IP) — step-2 of
                                                 two-step login; password verify.
- ``POST /set-password``              5  / 15 min (setup-session token, fallback IP)
- ``POST /api/mail-accounts``         10 / hour   (user_id)
- ``POST /api/mail-accounts/test``    10 / hour   (user_id)
- ``POST /api/mail-accounts/{}/sync-now``  5 / hour   (account_id)
- ``POST /api/messages/send``         30 / hour   (user_id)
- ``POST /api/admin/users*``          50 / hour   (user_id)
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


# Predeclared limits matching the docs table.
# ``LIMIT_LOGIN_USERNAME`` guards step-1 of the two-step login (ADR-0016).
# It is intentionally looser than ``LIMIT_LOGIN`` because the step-1 endpoint
# does not verify any secret and is only weakly enumerable (set-password vs
# password redirect).
LIMIT_LOGIN_USERNAME = Limit(name="login_user", capacity=30, window_seconds=15 * 60)
LIMIT_LOGIN = Limit(name="login", capacity=5, window_seconds=15 * 60)
LIMIT_SET_PASSWORD = Limit(name="setpwd", capacity=5, window_seconds=15 * 60)
LIMIT_ACCOUNT_TEST = Limit(name="acc_test", capacity=50, window_seconds=60 * 60)
LIMIT_ACCOUNT_WRITE = Limit(name="acc_write", capacity=50, window_seconds=60 * 60)
LIMIT_ACCOUNT_SYNC = Limit(name="acc_sync", capacity=5, window_seconds=60 * 60)
LIMIT_MESSAGE_SEND = Limit(name="msg_send", capacity=30, window_seconds=60 * 60)
LIMIT_ADMIN_WRITE = Limit(name="admin_write", capacity=50, window_seconds=60 * 60)
# Tags (ADR-0017): writes (create/edit/delete tag, add/remove rule) — 30/h.
# ``apply_to_existing`` is a heavier path and gets its own 50/h limit per user.
# Raised 5/h -> 50/h: bulk onboarding and repeated re-applies (debug / re-tagging)
# need headroom; the runaway-guard (>100k messages -> 422, ADR-0017 §7) still
# protects against expensive full-table scans regardless of this cap.
LIMIT_TAGS_WRITE = Limit(name="tags_write", capacity=30, window_seconds=60 * 60)
LIMIT_TAGS_APPLY = Limit(name="tags_apply", capacity=50, window_seconds=60 * 60)
# Telegram persistent SSO (ADR-0022 §1.2):
# - per IP:           30 / min  (front line — covers HMAC-fail flooding).
# - per tg_user_id:   10 / min  (post-HMAC — covers replay of valid init_data).
LIMIT_TG_AUTH_IP = Limit(name="tg_auth_ip", capacity=30, window_seconds=60)
LIMIT_TG_AUTH_USER = Limit(name="tg_auth_user", capacity=10, window_seconds=60)
# Multi-link management (ADR-0024 §4, docs/04-api-contracts.md §4b):
# add (POST) / unlink (DELETE) a TG link while authenticated — 10/h per user.
LIMIT_TG_LINKS_WRITE = Limit(name="tg_links_write", capacity=10, window_seconds=60 * 60)
# Outbound webhooks (ADR-0023 §5). All four limits are keyed per
# ``webhook_id`` except ``LIMIT_WEBHOOK_CREATE`` which is keyed per
# ``group_id`` (anti-spam for accidentally re-creating after delete).
LIMIT_WEBHOOK_CREATE = Limit(name="webhook_create", capacity=10, window_seconds=60 * 60)
LIMIT_WEBHOOK_UPDATE = Limit(name="webhook_update", capacity=30, window_seconds=60 * 60)
LIMIT_WEBHOOK_DELETE = Limit(name="webhook_delete", capacity=10, window_seconds=60 * 60)
LIMIT_WEBHOOK_ROTATE = Limit(name="webhook_rotate", capacity=5, window_seconds=60 * 60)
# ``LIMIT_WEBHOOK_TEST.capacity`` is overridden at consume-time by
# ``settings.WEBHOOK_TEST_LIMIT`` so operators can tune the cap without a
# redeploy of the codebase. The static value here is a sensible fallback
# only if the settings lookup is unavailable for some reason.
LIMIT_WEBHOOK_TEST = Limit(name="webhook_test", capacity=10, window_seconds=60 * 60)
# Telegram per-chat send throttle (ADR-0022 §2.9). ``capacity`` is overridden
# at consume-time from ``settings.TG_SEND_PER_CHAT_PER_MINUTE`` (same pattern as
# ``LIMIT_WEBHOOK_TEST``) so operators can tune the cap without a code redeploy.
# Consumed via the non-raising :func:`try_consume` (a throttled recipient is
# skipped this tick, not rejected with an error). Key: ``rl:tg_send:<chat_id>``.
LIMIT_TG_SEND_PER_CHAT = Limit(name="tg_send", capacity=20, window_seconds=60)
# External PULL-API (ADR-0029 §1/§4): 120 req / 60 s per client IP. Consumed
# FIRST in the router — before any work with the API key — so a brute-force /
# flood of failed-auth attempts is rate-limited too (anti-bruteforce + DoS).
# ``capacity`` is overridden at consume-time from
# ``settings.EXTERNAL_API_RATE_LIMIT_PER_MINUTE`` (same pattern as
# ``LIMIT_WEBHOOK_TEST`` / ``LIMIT_TG_SEND_PER_CHAT``) so operators can tune the
# cap without a code redeploy. The static value here is the default fallback.
LIMIT_EXTERNAL_API = Limit(name="external_api", capacity=120, window_seconds=60)


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


async def try_consume(limit: Limit, key: str) -> bool:
    """Non-blocking fixed-window check (ADR-0022 §2.9).

    Same ``INCR`` + ``EXPIRE(nx)`` mechanics as :func:`consume`, but instead
    of raising :class:`RateLimitedError` when the window budget is exhausted it
    returns ``False``; ``True`` while there is still budget (the counter is
    incremented either way). An empty ``key`` yields ``True`` (fail-open — same
    no-enforcement posture as :func:`consume`, which logs and returns).

    Used for the per-chat Telegram send throttle: a ``False`` result means the
    recipient is skipped this tick (the recovery scan picks the message up
    later), so it must not abort the dispatch loop.
    """
    if not key:
        # No key -> can't enforce; fail-open (don't drop the send).
        log.warning("rate_limit_no_key", limit_name=limit.name)
        return True
    redis = get_redis()
    redis_key = f"rl:{limit.name}:{key}"
    async with redis.pipeline(transaction=False) as pipe:
        pipe.incr(redis_key)
        pipe.expire(redis_key, limit.window_seconds, nx=True)
        results = await pipe.execute()
    current = int(results[0])
    return current <= limit.capacity


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
