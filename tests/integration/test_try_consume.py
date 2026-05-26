"""Integration tests for the non-raising :func:`try_consume` limiter
(ADR-0022 §2.9 — per-chat Telegram send throttle).

Source of truth: ``backend/app/rate_limit.py``.

Contract (spec item B):

- Returns ``True`` exactly ``capacity`` times, then ``False`` (counter still
  increments past capacity, but never raises).
- Never raises :class:`RateLimitedError` (unlike :func:`consume`).
- Empty ``key`` → ``True`` (fail-open — same no-enforcement posture as
  :func:`consume`).
- Independent keys are isolated from each other.
- After the fixed window expires the counter resets and budget is restored.
- ``LIMIT_TG_SEND_PER_CHAT`` capacity can be overridden at consume-time by a
  freshly-built :class:`Limit` (the override pattern used by the dispatcher to
  read ``settings.TG_SEND_PER_CHAT_PER_MINUTE`` without a code redeploy).
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from backend.app.exceptions import RateLimitedError
from backend.app.rate_limit import (
    LIMIT_TG_SEND_PER_CHAT,
    Limit,
    try_consume,
)
from shared.redis_client import get_redis

pytestmark = pytest.mark.integration


def _unique_key() -> str:
    return f"chat-{uuid.uuid4().hex[:12]}"


class TestTryConsumeCapacity:
    async def test_returns_true_exactly_capacity_times_then_false(self, redis_client: Any) -> None:
        limit = Limit(name="t_cap", capacity=3, window_seconds=60)
        key = _unique_key()
        results = [await try_consume(limit, key=key) for _ in range(5)]
        # First `capacity` are True, the rest False.
        assert results == [True, True, True, False, False], results

    async def test_does_not_raise_when_over_capacity(self, redis_client: Any) -> None:
        limit = Limit(name="t_noraise", capacity=1, window_seconds=60)
        key = _unique_key()
        assert await try_consume(limit, key=key) is True
        # Over the budget — must return False, NOT raise RateLimitedError.
        try:
            second = await try_consume(limit, key=key)
        except RateLimitedError as exc:  # pragma: no cover - failure path
            pytest.fail(f"try_consume must not raise, got {exc!r}")
        assert second is False


class TestTryConsumeFailOpen:
    async def test_empty_key_is_fail_open_true(self, redis_client: Any) -> None:
        limit = Limit(name="t_failopen", capacity=1, window_seconds=60)
        # No key → cannot enforce; returns True every time (never throttles).
        assert await try_consume(limit, key="") is True
        assert await try_consume(limit, key="") is True
        assert await try_consume(limit, key="") is True


class TestTryConsumeIsolation:
    async def test_independent_keys_have_independent_budgets(self, redis_client: Any) -> None:
        limit = Limit(name="t_iso", capacity=1, window_seconds=60)
        key_a = _unique_key()
        key_b = _unique_key()
        # Exhaust key_a.
        assert await try_consume(limit, key=key_a) is True
        assert await try_consume(limit, key=key_a) is False
        # key_b is untouched — still has its full budget.
        assert await try_consume(limit, key=key_b) is True
        assert await try_consume(limit, key=key_b) is False

    async def test_independent_limit_names_do_not_collide(self, redis_client: Any) -> None:
        # Same key string, different limit names → different Redis keys.
        key = _unique_key()
        limit_x = Limit(name="t_name_x", capacity=1, window_seconds=60)
        limit_y = Limit(name="t_name_y", capacity=1, window_seconds=60)
        assert await try_consume(limit_x, key=key) is True
        assert await try_consume(limit_x, key=key) is False
        # Different name → fresh budget for the same key.
        assert await try_consume(limit_y, key=key) is True


class TestTryConsumeWindowReset:
    async def test_window_expiry_resets_budget(self, redis_client: Any) -> None:
        """After the fixed window elapses the counter resets.

        We avoid ``time.sleep`` (flaky / slow): instead we drive the Redis key
        expiry deterministically by deleting it (equivalent to the window
        having elapsed — the next INCR starts a fresh window with EXPIRE nx).
        """
        limit = Limit(name="t_reset", capacity=1, window_seconds=60)
        key = _unique_key()
        assert await try_consume(limit, key=key) is True
        assert await try_consume(limit, key=key) is False

        # Simulate window expiry: drop the underlying Redis counter key.
        redis = get_redis()
        await redis.delete(f"rl:{limit.name}:{key}")

        # Budget restored.
        assert await try_consume(limit, key=key) is True

    async def test_expire_is_set_with_window_ttl(self, redis_client: Any) -> None:
        limit = Limit(name="t_ttl", capacity=5, window_seconds=42)
        key = _unique_key()
        await try_consume(limit, key=key)
        redis = get_redis()
        ttl = await redis.ttl(f"rl:{limit.name}:{key}")
        # TTL was set (positive) and does not exceed the window.
        assert 0 < int(ttl) <= 42


class TestTgSendPerChatOverride:
    async def test_capacity_override_from_settings_value(self, redis_client: Any) -> None:
        """The dispatcher rebuilds ``LIMIT_TG_SEND_PER_CHAT`` with a capacity
        taken from ``settings.TG_SEND_PER_CHAT_PER_MINUTE`` (override pattern).
        Verify a smaller override is honoured at consume-time.
        """
        override = Limit(
            name=LIMIT_TG_SEND_PER_CHAT.name,
            capacity=2,  # pretend settings.TG_SEND_PER_CHAT_PER_MINUTE == 2
            window_seconds=LIMIT_TG_SEND_PER_CHAT.window_seconds,
        )
        key = _unique_key()
        assert await try_consume(override, key=key) is True
        assert await try_consume(override, key=key) is True
        # Third call exceeds the overridden capacity of 2.
        assert await try_consume(override, key=key) is False

    async def test_static_limit_name_is_tg_send(self) -> None:
        # Sanity: the predeclared limit uses the documented Redis key prefix.
        assert LIMIT_TG_SEND_PER_CHAT.name == "tg_send"
        assert LIMIT_TG_SEND_PER_CHAT.window_seconds == 60
