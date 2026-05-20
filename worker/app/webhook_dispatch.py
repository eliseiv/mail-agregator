"""APScheduler job: drain the outbound-webhook dispatch queue
(ADR-0023 §3.3).

Tick cadence: ``settings.WEBHOOK_DISPATCH_INTERVAL_SECONDS`` (default 5s).
Max-instances 1 + coalesce: see ``worker/app/main.py``.

Per tick:

1. ``LPOP webhook_dispatch_queue count=BATCH_SIZE`` — drain up to N items.
2. For each item, call :meth:`WebhookDispatchService.dispatch_one_payload`
   which resolves recipients, POSTs, and handles 2xx/4xx/5xx/410/network.
3. Any failure inside the per-item call is caught and logged; the
   dispatcher must keep running so subsequent items are still processed.

Idempotency is delegated to the ``webhook_deliveries`` UNIQUE constraint
— even if Redis duplicates an item (recovery path), no receiver gets
the same event twice.
"""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Any, cast

from shared.config import get_settings
from shared.db import make_session
from shared.logging import get_logger
from shared.redis_client import get_redis

log = get_logger(__name__)

_QUEUE_KEY = "webhook_dispatch_queue"


async def webhook_dispatch() -> None:
    """One dispatcher tick."""
    settings = get_settings()
    redis = get_redis()
    batch_size = settings.WEBHOOK_BATCH_SIZE

    # ``LPOP key count=N`` returns ``[]`` (not None) for an empty list
    # when count is supplied. See the parallel implementation in
    # ``tg_notify_dispatch`` for the cast rationale.
    raw_items = await cast(
        Awaitable[bytes | str | list[Any] | None],
        redis.lpop(_QUEUE_KEY, count=batch_size),
    )
    if not raw_items:
        return

    if isinstance(raw_items, bytes | str):
        items: list[str] = [raw_items.decode() if isinstance(raw_items, bytes) else raw_items]
    else:
        items = [(it.decode() if isinstance(it, bytes) else it) for it in raw_items]

    log.info("webhook_dispatch_start", batch=len(items))

    # Local import to avoid pulling backend.app heavy graph at module load.
    from backend.app.webhooks.dispatch_service import WebhookDispatchService

    for raw in items:
        try:
            async with make_session() as s, s.begin():
                await WebhookDispatchService(s).dispatch_one_payload(raw)
        except Exception as exc:
            # Never propagate — APScheduler would log the traceback but
            # subsequent items would be skipped this tick. Log + carry on.
            log.warning(
                "webhook_dispatch_item_error",
                detail=str(exc)[:200],
                error_type=type(exc).__name__,
            )
