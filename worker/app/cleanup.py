"""Retention cleanup (ADR-0011): daily at 03:00 UTC, delete >30 day messages.

Algorithm: see ``docs/05-modules.md`` sec. 15.
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
from shared.storage import get_storage

log = get_logger(__name__)

_BATCH = 5000


@dataclass(slots=True)
class CleanupStats:
    deleted_messages: int
    deleted_attachments_minio: int


async def retention_cleanup() -> CleanupStats:
    """Delete messages and attachments older than ``RETENTION_DAYS``."""
    settings = get_settings()
    cycle_id = str(uuid.uuid4())
    structlog.contextvars.bind_contextvars(cycle_id=cycle_id)
    log.info("retention_cleanup_start", retention_days=settings.RETENTION_DAYS)

    storage = get_storage()
    threshold = datetime.now(UTC) - timedelta(days=settings.RETENTION_DAYS)
    total_deleted = 0
    total_obj_deleted = 0

    try:
        while True:
            # 1) Find a batch of expired messages.
            async with make_session() as s:
                rows = await MessagesRepo(s).select_expired(threshold, _BATCH)
            if not rows:
                break
            message_ids = [r[0] for r in rows]

            # 2) Collect S3 keys for attachments under those messages.
            async with make_session() as s:
                keys = await MessagesRepo(s).select_attachment_keys_for_messages(message_ids)

            # 3) Delete S3 objects (best-effort).
            if keys:
                try:
                    await storage.delete_objects(keys)
                    total_obj_deleted += len(keys)
                except Exception as exc:
                    log.warning(
                        "retention_cleanup_s3_partial_fail",
                        detail=str(exc)[:200],
                    )

            # 4) Delete messages (cascade -> attachments rows).
            async with make_session() as s, s.begin():
                deleted = await MessagesRepo(s).delete_messages(message_ids)
            total_deleted += deleted

            log.info(
                "retention_cleanup_batch",
                deleted=deleted,
                attachments=len(keys),
            )

        log.info(
            "retention_cleanup_finish",
            deleted_messages=total_deleted,
            deleted_attachments=total_obj_deleted,
        )
        return CleanupStats(
            deleted_messages=total_deleted,
            deleted_attachments_minio=total_obj_deleted,
        )
    finally:
        structlog.contextvars.unbind_contextvars("cycle_id")
