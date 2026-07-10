"""APScheduler job: re-enqueue messages not yet pushed to the CRM (ADR-0043 §2).

Tick cadence: ``settings.CRM_PUSH_RECOVERY_INTERVAL_SECONDS`` (default 1h).

A worker crash between the sync COMMIT and the ``crm_push_dispatch`` LPOP, or
a sustained CRM outage that outlives the queue, can leave messages with
``pushed_at IS NULL``. The recovery scan re-enqueues any such message fetched
within the lookback window (``CRM_PUSH_RECOVERY_WINDOW_HOURS``, bounded by the
retention window), capped at ``CRM_PUSH_RECOVERY_BATCH_SIZE`` per tick.

Idempotent: a message already sitting in the queue simply gets a second entry;
the CRM ingest dedups, and ``pushed_at`` is stamped once delivered so the next
scan skips it. Uses the ``ix_messages_pushed_at_pending`` partial index.
"""

from __future__ import annotations

from shared.config import get_settings
from shared.db import make_session
from shared.logging import get_logger


async def crm_push_recovery_scan() -> None:
    """One CRM push recovery-scan tick."""
    log = get_logger(__name__)
    settings = get_settings()

    from backend.app.crm_push.service import CrmPushService, enqueue_push_ids

    async with make_session() as s:
        candidate_ids = await CrmPushService(s).list_recovery_candidates(
            window_hours=settings.CRM_PUSH_RECOVERY_WINDOW_HOURS,
            limit=settings.CRM_PUSH_RECOVERY_BATCH_SIZE,
        )

    if not candidate_ids:
        return

    pushed = await enqueue_push_ids(candidate_ids, source="recovery")
    log.info(
        "crm_push_recovery_enqueued",
        candidates=len(candidate_ids),
        pushed=pushed,
        window_hours=settings.CRM_PUSH_RECOVERY_WINDOW_HOURS,
    )
