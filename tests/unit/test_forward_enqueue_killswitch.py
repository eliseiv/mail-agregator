"""ADR-0034 §6 kill-switch: the forward producer must NOT enqueue while the
feature is disabled.

When ``FORWARDING_ENABLED=false`` the consumer job (``worker.app.main``) is not
registered, so nothing drains ``forward_dispatch_queue``. If the producer kept
LPUSH-ing, the Redis list would grow unbounded. These tests pin the guard in
``ForwardDispatchService.enqueue_message_ids`` (the single enqueue call-site).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.app.forwarding import dispatch_service as ds

pytestmark = pytest.mark.unit


class _Settings:
    def __init__(self, enabled: bool) -> None:
        self.FORWARDING_ENABLED = enabled


def _patch(monkeypatch: pytest.MonkeyPatch, *, enabled: bool) -> MagicMock:
    redis = MagicMock()
    redis.lpush = AsyncMock()
    monkeypatch.setattr(ds, "get_settings", lambda: _Settings(enabled))
    monkeypatch.setattr(ds, "get_redis", lambda: redis)
    return redis


@pytest.mark.asyncio
async def test_enqueue_is_noop_when_flag_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    redis = _patch(monkeypatch, enabled=False)
    svc = ds.ForwardDispatchService(session=MagicMock())

    pushed = await svc.enqueue_message_ids([1, 2, 3])

    assert pushed == 0
    redis.lpush.assert_not_called()  # nothing lands on the un-drained queue


@pytest.mark.asyncio
async def test_enqueue_pushes_when_flag_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    redis = _patch(monkeypatch, enabled=True)
    svc = ds.ForwardDispatchService(session=MagicMock())

    pushed = await svc.enqueue_message_ids([1, 2, 3])

    assert pushed == 3
    redis.lpush.assert_awaited_once()


@pytest.mark.asyncio
async def test_enqueue_empty_is_noop_regardless_of_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    redis = _patch(monkeypatch, enabled=True)
    svc = ds.ForwardDispatchService(session=MagicMock())

    assert await svc.enqueue_message_ids([]) == 0
    redis.lpush.assert_not_called()
