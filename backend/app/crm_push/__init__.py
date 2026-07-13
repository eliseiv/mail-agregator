"""CRM push connector (ADR-0043 §2).

The aggregator, now a thin mail-connector, PUSHes every newly synced message
to the CRM (``POST {CRM_INGEST_URL}/api/mail/ingest``) and mirrors mailbox
sync-status changes (``POST {CRM_MAILBOX_STATUS_URL}/api/mail/mailbox-status``).
Both channels are authenticated by an HMAC-SHA256 signature over the *raw*
request body (see :func:`backend.app.crm_push.service.build_signature`).

Enqueue helpers (Redis, no DB session) live here so both the worker
(``sync_cycle`` / disable path) and the api (mailbox re-enable path) can push
onto the queues without pulling the heavy service graph.
"""

from __future__ import annotations

from backend.app.crm_push.service import (
    CRM_PUSH_QUEUE_KEY,
    CRM_STATUS_QUEUE_KEY,
    CrmPushService,
    CrmStatusService,
    build_signature,
    enqueue_crm_status,
    enqueue_crm_status_best_effort,
    enqueue_push_ids,
)

__all__ = [
    "CRM_PUSH_QUEUE_KEY",
    "CRM_STATUS_QUEUE_KEY",
    "CrmPushService",
    "CrmStatusService",
    "build_signature",
    "enqueue_crm_status",
    "enqueue_crm_status_best_effort",
    "enqueue_push_ids",
]
