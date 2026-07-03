"""Forward dispatch producer (ADR-0034 §3.1).

:meth:`ForwardDispatchService.enqueue_message_ids` LPUSHes new ``message_id``
values onto the Redis ``forward_dispatch_queue`` after ``sync_cycle`` commits
(symmetric to :meth:`WebhookDispatchService.enqueue_message_ids`). The
consumer lives in :mod:`worker.app.forward_dispatch`.

The wire format (``_QueuePayload``) is a fork of the webhook / TG payload so
operational reasoning stays symmetric across the three queues.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Final, cast

from sqlalchemy.ext.asyncio import AsyncSession

from shared.config import get_settings
from shared.redis_client import get_redis

FORWARD_DISPATCH_QUEUE_KEY: Final[str] = "forward_dispatch_queue"
_PAYLOAD_VERSION: Final[int] = 1


@dataclass(frozen=True, slots=True)
class _QueuePayload:
    """Wire format of items in ``forward_dispatch_queue``."""

    message_id: int
    source: str  # "sync"

    @classmethod
    def from_json(cls, raw: str) -> _QueuePayload | None:
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return None
        if not isinstance(data, dict):
            return None
        mid_raw = data.get("message_id")
        if not isinstance(mid_raw, int):
            return None
        source = data.get("source")
        if not isinstance(source, str):
            source = "sync"
        return cls(message_id=int(mid_raw), source=source)

    def to_json(self) -> str:
        return json.dumps(
            {
                "v": _PAYLOAD_VERSION,
                "message_id": self.message_id,
                "source": self.source,
            },
            separators=(",", ":"),
        )


class ForwardDispatchService:
    def __init__(self, session: AsyncSession) -> None:
        self._db = session

    async def enqueue_message_ids(self, message_ids: list[int]) -> int:
        """LPUSH ``message_id`` entries from ``sync_cycle``.

        Returns the number of items pushed. Callers (worker ``sync_cycle``)
        wrap this in try/except — a failure to LPUSH must never abort the
        sync cycle. The consumer performs the final config / loop / history
        checks, so a redundant enqueue is harmless (dedup via
        ``message_forwards``).

        ADR-0034 §6 kill-switch: when ``FORWARDING_ENABLED`` is off the
        consumer job is NOT registered (``worker.app.main``), so nothing would
        drain the queue. Gating the producer here — the single enqueue
        call-site — keeps the invariant in one place and prevents the
        ``forward_dispatch_queue`` from growing unbounded while disabled.
        """
        if not message_ids or not get_settings().FORWARDING_ENABLED:
            return 0
        redis = get_redis()
        items = [_QueuePayload(message_id=int(mid), source="sync").to_json() for mid in message_ids]
        await cast(Awaitable[int], redis.lpush(FORWARD_DISPATCH_QUEUE_KEY, *items))
        return len(items)
