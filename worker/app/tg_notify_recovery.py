"""APScheduler job: scan for un-delivered Telegram notifications
(ADR-0022 §2.8).

Tick cadence: ``settings.TG_NOTIFY_RECOVERY_INTERVAL_SECONDS`` (default 1h).

A worker crash between LPUSH (in ``sync_cycle``) and LPOP (in the
dispatcher) loses queue items. The recovery scan re-enqueues any
``messages.id`` that:

- was fetched within the lookback window
  (``TG_NOTIFY_RECOVERY_WINDOW_HOURS``, default 24h),
- has at least one ``message_tags`` row,
- has NO ``telegram_notifications`` row at all (no recipient was even
  reserved yet — distinct from "dispatcher tried but Bot API was sad",
  which leaves a row behind).

Bounded by ``TG_NOTIFY_RECOVERY_BATCH_SIZE`` per tick so a long outage
doesn't flood the queue. Subsequent ticks pick up the rest.
"""

from __future__ import annotations

from shared.config import get_settings
from shared.db import make_session
from shared.logging import get_logger

log = get_logger(__name__)


async def tg_notify_recovery_scan() -> None:
    """One recovery-scan tick."""
    settings = get_settings()

    # Local imports — same reasoning as in tg_notify_dispatch.
    from backend.app.repositories.telegram_notifications import (
        TelegramNotificationsRepo,
    )
    from backend.app.telegram.notify_service import TelegramNotifyService

    async with make_session() as s:
        repo = TelegramNotificationsRepo(s)
        candidate_ids = await repo.list_missing_for_recovery(
            window_hours=settings.TG_NOTIFY_RECOVERY_WINDOW_HOURS,
            limit=settings.TG_NOTIFY_RECOVERY_BATCH_SIZE,
        )

    if not candidate_ids:
        return

    async with make_session() as s:
        pushed = await TelegramNotifyService(s).enqueue_recovery(candidate_ids)

    log.info(
        "tg_notify_recovery_enqueued",
        candidates=len(candidate_ids),
        pushed=pushed,
        window_hours=settings.TG_NOTIFY_RECOVERY_WINDOW_HOURS,
    )
