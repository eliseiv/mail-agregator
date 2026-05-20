"""APScheduler job: scan for un-delivered outbound webhooks
(ADR-0023 §3.5).

Tick cadence: ``settings.WEBHOOK_RECOVERY_INTERVAL_SECONDS`` (default 1h).

A worker crash between LPUSH (in ``sync_cycle``) and LPOP (in the
dispatcher) loses queue items. The recovery scan re-enqueues any
``messages.id`` that:

- was fetched within the lookback window
  (``WEBHOOK_RECOVERY_WINDOW_HOURS``, default 24h),
- has at least one ``message_tags`` row,
- belongs to a team with an active, non-dead webhook whose
  ``created_at`` is older than the message's ``internal_date``
  (history-flood filter),
- has NO ``webhook_deliveries`` row at all (no recipient claimed —
  distinct from "tried but receiver said 4xx", which leaves a row
  behind with ``sent_at IS NOT NULL``).

Bounded by ``WEBHOOK_RECOVERY_BATCH_SIZE`` per tick so a long outage
doesn't flood the queue. Subsequent ticks pick up the rest.
"""

from __future__ import annotations

from shared.config import get_settings
from shared.db import make_session
from shared.logging import get_logger

log = get_logger(__name__)


async def webhook_recovery_scan() -> None:
    """One recovery-scan tick."""
    settings = get_settings()

    # Local imports — same reasoning as in webhook_dispatch.
    from backend.app.repositories.webhooks import WebhookDeliveriesRepo
    from backend.app.webhooks.dispatch_service import WebhookDispatchService

    async with make_session() as s:
        repo = WebhookDeliveriesRepo(s)
        candidate_ids = await repo.list_missing_for_recovery(
            window_hours=settings.WEBHOOK_RECOVERY_WINDOW_HOURS,
            limit=settings.WEBHOOK_RECOVERY_BATCH_SIZE,
        )

    if not candidate_ids:
        return

    async with make_session() as s:
        pushed = await WebhookDispatchService(s).enqueue_recovery(candidate_ids)

    log.info(
        "webhook_recovery_enqueued",
        candidates=len(candidate_ids),
        pushed=pushed,
        window_hours=settings.WEBHOOK_RECOVERY_WINDOW_HOURS,
    )
