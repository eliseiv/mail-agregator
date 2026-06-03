"""Integration: Outlook OAuth authorize rate-limit boundary (30/h per user).

Source of truth: ``docs/04-api-contracts.md`` §4c (authorize ``30 / час per
user``, key = ``user_id``) and ``backend/app/oauth/router.py``
(``LIMIT_OAUTH_AUTHORIZE`` consumed via :func:`backend.app.rate_limit.consume`).

We exercise the limiter at the helper level (``consume(LIMIT_OAUTH_AUTHORIZE,
user_key)``) against real Redis — the same code path the ``/authorize`` route
takes — to assert the window boundary deterministically without standing up the
full OAuth flow (which additionally needs an Azure App / mocked token endpoint):

- 30 consumes inside one window succeed,
- the 31st raises :class:`RateLimitedError` ("Rate limit exceeded."),
- the callback limit (``LIMIT_OAUTH_CALLBACK``) is keyed independently and is
  untouched by exhausting the authorize budget.

Requires Redis (docker compose up) — marked ``integration`` and skipped by the
``redis_client`` fixture when the dependency is absent.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from backend.app.exceptions import RateLimitedError
from backend.app.oauth.router import LIMIT_OAUTH_AUTHORIZE, LIMIT_OAUTH_CALLBACK
from backend.app.rate_limit import consume
from shared.redis_client import get_redis

pytestmark = pytest.mark.integration


def _user_key() -> str:
    return f"u-{uuid.uuid4().hex[:12]}"


class TestAuthorizeRateLimitBoundary:
    async def test_thirty_consumes_pass_thirty_first_raises(self, redis_client: Any) -> None:
        key = _user_key()
        # The first `capacity` (30) consumes must all succeed silently.
        for _ in range(LIMIT_OAUTH_AUTHORIZE.capacity):
            await consume(LIMIT_OAUTH_AUTHORIZE, key)  # calls 0..29 -> no raise
        # The 31st consume exceeds capacity -> 429 Rate limit exceeded.
        with pytest.raises(RateLimitedError) as exc_info:
            await consume(LIMIT_OAUTH_AUTHORIZE, key)
        assert "Rate limit exceeded" in str(exc_info.value)

    async def test_exactly_at_capacity_does_not_raise(self, redis_client: Any) -> None:
        # Boundary: the 30th call (current == capacity) is allowed.
        key = _user_key()
        for _ in range(29):
            await consume(LIMIT_OAUTH_AUTHORIZE, key)
        # 30th — current == 30 == capacity, current > capacity is False -> OK.
        await consume(LIMIT_OAUTH_AUTHORIZE, key)

    async def test_independent_users_have_independent_budgets(self, redis_client: Any) -> None:
        key_a = _user_key()
        key_b = _user_key()
        # Exhaust user A's authorize budget.
        for _ in range(LIMIT_OAUTH_AUTHORIZE.capacity):
            await consume(LIMIT_OAUTH_AUTHORIZE, key_a)
        with pytest.raises(RateLimitedError):
            await consume(LIMIT_OAUTH_AUTHORIZE, key_a)
        # User B is untouched — still has the full 30 budget.
        for _ in range(LIMIT_OAUTH_AUTHORIZE.capacity):
            await consume(LIMIT_OAUTH_AUTHORIZE, key_b)
        with pytest.raises(RateLimitedError):
            await consume(LIMIT_OAUTH_AUTHORIZE, key_b)


class TestCallbackLimitUnaffected:
    async def test_callback_budget_isolated_from_authorize(self, redis_client: Any) -> None:
        # Same key string, different Limit name (oauth_callback) => different
        # Redis key, so exhausting authorize must not touch the callback budget.
        key = _user_key()
        for _ in range(LIMIT_OAUTH_AUTHORIZE.capacity):
            await consume(LIMIT_OAUTH_AUTHORIZE, key)
        with pytest.raises(RateLimitedError):
            await consume(LIMIT_OAUTH_AUTHORIZE, key)

        # Callback (30/min per IP) still has its full budget for the same key.
        for _ in range(LIMIT_OAUTH_CALLBACK.capacity):
            await consume(LIMIT_OAUTH_CALLBACK, key)
        with pytest.raises(RateLimitedError):
            await consume(LIMIT_OAUTH_CALLBACK, key)

    async def test_window_reset_restores_budget(self, redis_client: Any) -> None:
        # Deterministic window expiry: drop the Redis counter key (equivalent
        # to the fixed window having elapsed) — avoids a flaky time.sleep.
        key = _user_key()
        for _ in range(LIMIT_OAUTH_AUTHORIZE.capacity):
            await consume(LIMIT_OAUTH_AUTHORIZE, key)
        with pytest.raises(RateLimitedError):
            await consume(LIMIT_OAUTH_AUTHORIZE, key)

        redis = get_redis()
        await redis.delete(f"rl:{LIMIT_OAUTH_AUTHORIZE.name}:{key}")

        # Budget restored — a fresh window allows the full 30 again.
        for _ in range(LIMIT_OAUTH_AUTHORIZE.capacity):
            await consume(LIMIT_OAUTH_AUTHORIZE, key)
        with pytest.raises(RateLimitedError):
            await consume(LIMIT_OAUTH_AUTHORIZE, key)
