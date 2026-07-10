"""Integration tests for the CRM-push queue dispatcher (ADR-0043 §2).

Real Redis (``redis_client`` fixture). Cover: LPOP -> push -> (on non-ok) re-enqueue with
``source=recovery``, queue drained on success, no-op on an empty queue, malformed items
skipped. ``CrmPushService.push_message_ids`` is monkeypatched (no network / DB). Plus the
enqueue gating: ``enqueue_push_ids([])`` writes nothing to Redis.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from backend.app.crm_push.service import (
    CRM_PUSH_QUEUE_KEY,
    CrmPushService,
    PushResult,
    _PushQueuePayload,
    enqueue_push_ids,
)
from worker.app.crm_push_dispatch import crm_push_dispatch

pytestmark = pytest.mark.integration  # needs Redis (+ DB for make_session)


async def _qlen(redis_client: Any) -> int:
    return int(await cast(Any, redis_client.llen(CRM_PUSH_QUEUE_KEY)))


def _patch_push(monkeypatch: pytest.MonkeyPatch, *, ok: bool) -> None:
    async def _fake(self: CrmPushService, message_ids: list[int]) -> PushResult:
        return PushResult(ok=ok, delivered=len(message_ids), marked=len(message_ids), missing=0)

    monkeypatch.setattr(CrmPushService, "push_message_ids", _fake)


async def test_dispatch_drains_queue_on_success(
    redis_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    await enqueue_push_ids([1, 2, 3], source="sync")
    assert await _qlen(redis_client) == 3
    _patch_push(monkeypatch, ok=True)

    await crm_push_dispatch()
    # success -> queue empty, nothing re-enqueued.
    assert await _qlen(redis_client) == 0


async def test_dispatch_reenqueues_on_failure(
    redis_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    await enqueue_push_ids([10, 11], source="sync")
    _patch_push(monkeypatch, ok=False)

    await crm_push_dispatch()
    # failure -> the same ids are back on the queue (source=recovery) for the next tick.
    assert await _qlen(redis_client) == 2
    items = await cast(Any, redis_client.lrange(CRM_PUSH_QUEUE_KEY, 0, -1))
    parsed = [
        _PushQueuePayload.from_json(it.decode() if isinstance(it, bytes) else it) for it in items
    ]
    ids = {p.message_id for p in parsed if p is not None}
    assert ids == {10, 11}
    assert all(p is not None and p.source == "recovery" for p in parsed)


async def test_dispatch_empty_queue_is_noop(
    redis_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    called = False

    async def _fake(self: CrmPushService, message_ids: list[int]) -> PushResult:
        nonlocal called
        called = True
        return PushResult(ok=True, delivered=0, marked=0, missing=0)

    monkeypatch.setattr(CrmPushService, "push_message_ids", _fake)
    await crm_push_dispatch()
    assert called is False  # empty queue -> push not called


async def test_dispatch_skips_malformed_items(
    redis_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # push a valid item + garbage; garbage is skipped, valid id is delivered.
    await enqueue_push_ids([42], source="sync")
    await cast(Any, redis_client.lpush(CRM_PUSH_QUEUE_KEY, "{not json"))
    seen: list[int] = []

    async def _fake(self: CrmPushService, message_ids: list[int]) -> PushResult:
        seen.extend(message_ids)
        return PushResult(ok=True, delivered=len(message_ids), marked=len(message_ids), missing=0)

    monkeypatch.setattr(CrmPushService, "push_message_ids", _fake)
    await crm_push_dispatch()
    assert seen == [42]  # only the valid id reached push
    assert await _qlen(redis_client) == 0


# ------------------------------------------------------ enqueue gating (no write)
async def test_enqueue_empty_does_not_touch_redis(redis_client: Any) -> None:
    pushed = await enqueue_push_ids([], source="sync")
    assert pushed == 0
    assert await _qlen(redis_client) == 0


async def test_enqueue_returns_count(redis_client: Any) -> None:
    pushed = await enqueue_push_ids([1, 2, 3])
    assert pushed == 3
    assert await _qlen(redis_client) == 3
