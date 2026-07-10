"""APScheduler job: drain the CRM mailbox-status queue (ADR-0043 §2).

Tick cadence: ``settings.CRM_STATUS_DISPATCH_INTERVAL_SECONDS`` (default 5s).
``max_instances=1`` + ``coalesce`` + registered only when
``settings.crm_status_enabled`` — see ``worker/app/main.py``.

Status events are enqueued on a mailbox disable transition
(``worker.sync_cycle._disable_after_failures``) and on re-enable
(``backend.accounts.service`` activate / creds-changed branches). Each queue
item carries only the ``mail_account_id``; the dispatcher loads the current
row and POSTs its live status snapshot to
``{CRM_MAILBOX_STATUS_URL}/api/mail/mailbox-status`` (HMAC-signed). The CRM
mirrors current state and dedups the down-alert on its side
(``down_alert_sent_at``), so ordering / staleness is irrelevant.

Per tick:

1. ``LPOP crm_status_queue count=CRM_STATUS_BATCH_SIZE``.
2. For each id, POST the current snapshot.
3. Non-2xx / transport error → re-enqueue that id for the next tick.

Any failure is caught + logged; the dispatcher must keep running.
"""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Any, cast

from shared.config import get_settings
from shared.db import make_session
from shared.logging import get_logger
from shared.redis_client import get_redis

log = get_logger(__name__)


async def crm_status_dispatch() -> None:
    """One CRM status-dispatcher tick."""
    settings = get_settings()
    redis = get_redis()

    from backend.app.crm_push.service import (
        CRM_STATUS_QUEUE_KEY,
        CrmStatusService,
        enqueue_crm_status,
        parse_status_payload,
    )

    raw_items = await cast(
        Awaitable[bytes | str | list[Any] | None],
        redis.lpop(CRM_STATUS_QUEUE_KEY, count=settings.CRM_STATUS_BATCH_SIZE),
    )
    if not raw_items:
        return

    if isinstance(raw_items, bytes | str):
        items: list[str] = [raw_items.decode() if isinstance(raw_items, bytes) else raw_items]
    else:
        items = [(it.decode() if isinstance(it, bytes) else it) for it in raw_items]

    account_ids: list[int] = []
    for raw in items:
        acc_id = parse_status_payload(raw)
        if acc_id is None:
            log.warning("crm_status_dispatch_malformed", raw_excerpt=raw[:200])
            continue
        account_ids.append(acc_id)

    if not account_ids:
        return

    log.info("crm_status_dispatch_start", batch=len(account_ids))

    for acc_id in account_ids:
        try:
            async with make_session() as s:
                ok = await CrmStatusService(s).push_status(acc_id)
        except Exception as exc:
            # Never propagate — subsequent items must still be processed.
            log.warning(
                "crm_status_dispatch_item_error",
                mail_account_id=acc_id,
                detail=str(exc)[:200],
                error_type=type(exc).__name__,
            )
            ok = False
        if not ok:
            try:
                await enqueue_crm_status(acc_id)
            except Exception as exc:
                log.warning(
                    "crm_status_reenqueue_failed",
                    mail_account_id=acc_id,
                    detail=str(exc)[:200],
                )
