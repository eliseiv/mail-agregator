"""Unit tests for the flash store (RPUSH+TTL on write, atomic LRANGE+DEL on
read, anonymous request = no-op).

Source of truth: ``backend/app/flash.py``.

Uses fakeredis so we don't depend on a live Redis for unit tests.
"""

from __future__ import annotations

from typing import Any

import fakeredis.aioredis
import pytest
from starlette.requests import Request

from backend.app import flash as flash_mod

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _patch_redis(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Replace ``shared.redis_client.get_redis`` with fakeredis."""
    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(flash_mod, "get_redis", lambda: fake)
    return fake


def _build_request(*, session_token: str | None, setup_token: str | None = None) -> Request:
    """Synthesize a minimal Starlette Request with state + cookies."""
    cookies = {}
    if setup_token:
        cookies["mas_setup"] = setup_token
    raw_cookies = "; ".join(f"{k}={v}" for k, v in cookies.items())
    scope: dict[str, Any] = {
        "type": "http",
        "method": "POST",
        "path": "/",
        "headers": [(b"cookie", raw_cookies.encode())] if raw_cookies else [],
    }
    # ``request.state`` lazily wraps ``scope["state"]`` (a plain dict) in a
    # Starlette ``State`` proxy. Pre-populate the dict; ``State`` reads it
    # via ``self._state[key]`` so it must be a dict, not a ``State`` instance.
    scope["state"] = {"session_token": session_token}
    return Request(scope)


class TestFlash:
    async def test_anonymous_no_op(self, _patch_redis: Any) -> None:
        req = _build_request(session_token=None)
        await flash_mod.flash(req, "success", "anything")
        # Nothing was pushed.
        keys = await _patch_redis.keys("flash:*")
        assert keys == []

    async def test_writes_with_ttl(self, _patch_redis: Any) -> None:
        req = _build_request(session_token="sess-tok-1")
        await flash_mod.flash(req, "success", "Created!")
        ttl = await _patch_redis.ttl("flash:sess-tok-1")
        # TTL must be in (0, FLASH_TTL_SECONDS].
        assert 0 < ttl <= flash_mod.FLASH_TTL_SECONDS

    async def test_consume_returns_and_clears(self, _patch_redis: Any) -> None:
        req = _build_request(session_token="sess-tok-2")
        await flash_mod.flash(req, "success", "Hello")
        await flash_mod.flash(req, "error", "Oops")
        result = await flash_mod.consume_flashes(req)
        assert [r["text"] for r in result] == ["Hello", "Oops"]
        # Key gone now.
        assert await _patch_redis.exists("flash:sess-tok-2") == 0

    async def test_consume_when_empty(self, _patch_redis: Any) -> None:
        req = _build_request(session_token="empty-sess")
        assert await flash_mod.consume_flashes(req) == []

    async def test_consume_anonymous_returns_empty(self, _patch_redis: Any) -> None:
        req = _build_request(session_token=None)
        assert await flash_mod.consume_flashes(req) == []

    async def test_setup_session_used_when_no_full_session(self, _patch_redis: Any) -> None:
        # When the full session is absent but the password-setup cookie is
        # present (e.g. after first-login), flash uses the setup token.
        req = _build_request(session_token=None, setup_token="setup-tok")
        await flash_mod.flash(req, "info", "Welcome")
        consumed = await flash_mod.consume_flashes(req)
        assert consumed == [{"category": "info", "text": "Welcome"}]

    async def test_invalid_category_raises(self, _patch_redis: Any) -> None:
        req = _build_request(session_token="sess")
        with pytest.raises(ValueError, match="unknown flash category"):
            await flash_mod.flash(req, "bogus", "x")  # type: ignore[arg-type]

    async def test_corrupt_payload_skipped_unknown_category_normalised(
        self, _patch_redis: Any
    ) -> None:
        # Inject a malformed entry directly.
        await _patch_redis.rpush("flash:sess-bad", "this is not json")
        await _patch_redis.rpush("flash:sess-bad", '{"category":"hax","text":"weird"}')
        req = _build_request(session_token="sess-bad")
        result = await flash_mod.consume_flashes(req)
        # Bad json dropped; unknown category normalised to "info".
        assert result == [{"category": "info", "text": "weird"}]
