"""APScheduler job: drain the CRM ingest push queue (ADR-0043 §2).

Tick cadence: ``settings.CRM_PUSH_DISPATCH_INTERVAL_SECONDS`` (default 5s).
``max_instances=1`` + ``coalesce`` + registered only when
``settings.crm_push_enabled`` — see ``worker/app/main.py``.

Per tick:

1. ``LPOP crm_push_queue count=CRM_PUSH_BATCH_SIZE`` — drain up to N ids.
2. POST the loaded messages to ``{CRM_INGEST_URL}/api/mail/ingest`` in ONE
   batch (HMAC-signed over the raw body).
3. ``2xx`` → stamp ``pushed_at=now()`` on the delivered rows.
4. Non-2xx / transport error → re-enqueue the ids (``source=recovery``) so the
   next tick retries. The CRM ingest is idempotent
   (``ON CONFLICT (mail_account_id, uidvalidity, uid) DO NOTHING``), so a
   re-push never duplicates a message.

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


async def crm_push_dispatch() -> None:
    """One CRM ingest-dispatcher tick."""
    settings = get_settings()
    redis = get_redis()

    from backend.app.crm_push.service import (
        CRM_PUSH_QUEUE_KEY,
        CrmPushService,
        _PushQueuePayload,
        enqueue_push_ids,
    )

    raw_items = await cast(
        Awaitable[bytes | str | list[Any] | None],
        redis.lpop(CRM_PUSH_QUEUE_KEY, count=settings.CRM_PUSH_BATCH_SIZE),
    )
    if not raw_items:
        return

    if isinstance(raw_items, bytes | str):
        items: list[str] = [raw_items.decode() if isinstance(raw_items, bytes) else raw_items]
    else:
        items = [(it.decode() if isinstance(it, bytes) else it) for it in raw_items]

    message_ids: list[int] = []
    for raw in items:
        payload = _PushQueuePayload.from_json(raw)
        if payload is None:
            log.warning("crm_push_dispatch_malformed", raw_excerpt=raw[:200])
            continue
        message_ids.append(payload.message_id)

    if not message_ids:
        return

    log.info("crm_push_dispatch_start", batch=len(message_ids))

    async with make_session() as s, s.begin():
        result = await CrmPushService(s).push_message_ids(message_ids)

    if not result.ok:
        # Re-enqueue for the next tick — never re-raise (best-effort).
        try:
            await enqueue_push_ids(message_ids, source="recovery")
        except Exception as exc:
            log.warning(
                "crm_push_reenqueue_failed",
                detail=str(exc)[:200],
                count=len(message_ids),
            )
