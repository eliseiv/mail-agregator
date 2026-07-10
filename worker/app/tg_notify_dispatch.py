"""APScheduler job: drain the Telegram notification queue (ADR-0022 §2.4).

Tick cadence: ``settings.TG_NOTIFY_DISPATCH_INTERVAL_SECONDS`` (default 5s).
Max-instances 1 + coalesce: see ``worker/app/main.py``.

Per tick:

1. ``LPOP tg_notify_queue count=BATCH_SIZE`` — drain up to N items.
2. For each item, call :meth:`TelegramNotifyService.dispatch_one_payload`
   which resolves recipients and sends Bot API calls.
3. Any failure is caught and logged; the dispatcher must keep running.

Idempotency is delegated to the ``telegram_notifications`` UNIQUE
constraint — even if Redis duplicates an item (e.g. retry path), no
recipient receives the same notification twice.
"""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Any, cast

from shared.config import get_settings
from shared.db import make_session
from shared.logging import get_logger
from shared.redis_client import get_redis

log = get_logger(__name__)

_QUEUE_KEY = "tg_notify_queue"


async def tg_notify_dispatch() -> None:
    """One dispatcher tick."""
    settings = get_settings()
    # ADR-0043 cut-over kill-switch: when Telegram delivery is muted, drop this
    # tick silently (the queue keeps filling; nothing is sent).
    if not settings.TELEGRAM_DELIVERY_ENABLED:
        return
    redis = get_redis()
    batch_size = settings.TG_NOTIFY_BATCH_SIZE

    # ``LPOP key count=N`` returns ``[]`` (not None) for an empty list when
    # count is supplied. redis-py types the awaited result as
    # ``Awaitable[T] | T`` (sync/async union); the runtime here is always
    # async — ``cast`` picks the async branch in a way that satisfies both
    # local mypy and the stricter CI mypy.
    raw_items = await cast(
        Awaitable[bytes | str | list[Any] | None],
        redis.lpop(_QUEUE_KEY, count=batch_size),
    )
    if not raw_items:
        return

    if isinstance(raw_items, bytes | str):
        # Defensive: some redis clients return a scalar for count=1.
        items: list[str] = [raw_items.decode() if isinstance(raw_items, bytes) else raw_items]
    else:
        items = [(it.decode() if isinstance(it, bytes) else it) for it in raw_items]

    log.info("tg_notify_dispatch_start", batch=len(items))

    # Local import to avoid pulling backend.app heavy graph at module load.
    from backend.app.telegram.notify_service import TelegramNotifyService

    for raw in items:
        try:
            async with make_session() as s, s.begin():
                await TelegramNotifyService(s).dispatch_one_payload(raw)
        except Exception as exc:
            # Never propagate — APScheduler would log the traceback but
            # subsequent items would be skipped this tick. Log + carry on.
            log.warning(
                "tg_notify_dispatch_item_error",
                detail=str(exc)[:200],
                error_type=type(exc).__name__,
            )
