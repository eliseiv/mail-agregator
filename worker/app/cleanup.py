"""Retention cleanup (ADR-0011): daily at 03:00 UTC, delete >30 day messages.

ADR-0044 §4 (phase A3): the MinIO attachment cleanup is gone — attachments are
neither fetched nor stored (ADR-0043 §4). Retention now prunes ``messages``
only — the push-outbox working buffer (the durable store is the CRM).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import structlog

# See ``worker/app/__init__.py`` for the rationale on importing from
# ``backend.app.*`` at the top of worker modules.
from backend.app.repositories.messages import MessagesRepo
from shared.config import get_settings
from shared.db import make_session
from shared.logging import get_logger

log = get_logger(__name__)

_BATCH = 5000


@dataclass(slots=True)
class CleanupStats:
    deleted_messages: int


async def retention_cleanup() -> CleanupStats:
    """Delete messages older than ``RETENTION_DAYS``."""
    settings = get_settings()
    cycle_id = str(uuid.uuid4())
    structlog.contextvars.bind_contextvars(cycle_id=cycle_id)
    log.info("retention_cleanup_start", retention_days=settings.RETENTION_DAYS)

    threshold = datetime.now(UTC) - timedelta(days=settings.RETENTION_DAYS)
    total_deleted = 0

    try:
        while True:
            # 1) Find a batch of expired messages.
            async with make_session() as s:
                rows = await MessagesRepo(s).select_expired(threshold, _BATCH)
            if not rows:
                break
            message_ids = [r[0] for r in rows]

            # 2) Delete messages.
            async with make_session() as s, s.begin():
                deleted = await MessagesRepo(s).delete_messages(message_ids)
            total_deleted += deleted

            log.info("retention_cleanup_batch", deleted=deleted)

        log.info("retention_cleanup_finish", deleted_messages=total_deleted)
        return CleanupStats(deleted_messages=total_deleted)
    finally:
        structlog.contextvars.unbind_contextvars("cycle_id")
