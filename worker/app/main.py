"""Worker entrypoint: APScheduler (connector, ADR-0044 §4, phase A3).

Jobs left: ``sync_cycle``, ``force_sync_dispatcher``, ``retention_cleanup``,
``alive_touch``, ``crm_push_dispatch``, ``crm_push_recovery``,
``crm_status_dispatch``. Removed with their subsystems (ADR-0043 §4):
``tg_notify_dispatch`` / ``tg_notify_recovery`` / ``push_notify_dispatch`` /
``mailbox_alert_dispatch`` / ``webhook_dispatch`` / ``webhook_recovery`` /
``forward_dispatch`` and the ``ensure_bucket`` bootstrap (MinIO).

Started by ``deploy/Dockerfile`` (target=worker): ``python -m worker.app.main``.

Jobs (ADR-0003):
- ``sync_cycle`` — every ``SYNC_INTERVAL_MINUTES`` (default 5). Polls all
  active mailboxes.
- ``force_sync_dispatcher`` — every 10s. Drains ``force_sync:{id}`` Redis
  markers written by ``POST /api/external/mailboxes/{id}/sync`` so an
  on-demand sync requested by the CRM lands in under 10 seconds without
  lowering the regular poll cadence.
- ``retention_cleanup`` — daily at 03:00 UTC.
- ``alive_touch`` — every 30s; healthcheck reads ``mtime`` of ``/tmp/worker_alive``.

Graceful shutdown: SIGTERM/SIGINT -> ``scheduler.shutdown(wait=True)``.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import sys
import time
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from shared.config import get_settings
from shared.db import dispose_engine, init_engine
from shared.logging import configure_logging, get_logger
from shared.redis_client import close_redis
from worker.app.cleanup import retention_cleanup
from worker.app.crm_push_dispatch import crm_push_dispatch
from worker.app.crm_push_recovery import crm_push_recovery_scan
from worker.app.crm_status_dispatch import crm_status_dispatch
from worker.app.sync_cycle import force_sync_dispatch, sync_cycle

log = get_logger(__name__)

ALIVE_FILE = Path("/tmp/worker_alive")  # — required by docker healthcheck


def _touch_alive() -> None:
    """Update mtime on ``/tmp/worker_alive`` for the docker healthcheck."""
    try:
        ALIVE_FILE.parent.mkdir(parents=True, exist_ok=True)
        ALIVE_FILE.touch(exist_ok=True)
        os.utime(ALIVE_FILE, (time.time(), time.time()))
    except OSError as exc:
        log.warning("alive_touch_fail", detail=str(exc)[:200])


async def _safe_sync_cycle() -> None:
    """Wrapper that never propagates — APScheduler suppresses but we want to log."""
    try:
        await sync_cycle()
    except Exception as exc:
        log.error(
            "sync_cycle_unhandled",
            detail=str(exc)[:300],
            exc_info=True,
        )


async def _safe_force_sync_dispatch() -> None:
    """Wrapper for the 10s force-sync dispatcher."""
    try:
        await force_sync_dispatch()
    except Exception as exc:
        log.error(
            "force_sync_dispatch_unhandled",
            detail=str(exc)[:300],
            exc_info=True,
        )


async def _safe_cleanup() -> None:
    try:
        await retention_cleanup()
    except Exception as exc:
        log.error(
            "retention_cleanup_unhandled",
            detail=str(exc)[:300],
            exc_info=True,
        )


async def _safe_crm_push_dispatch() -> None:
    """Wrapper for the CRM ingest-push dispatcher (ADR-0043 §2)."""
    try:
        await crm_push_dispatch()
    except Exception as exc:
        log.error(
            "crm_push_dispatch_unhandled",
            detail=str(exc)[:300],
            exc_info=True,
        )


async def _safe_crm_push_recovery() -> None:
    """Wrapper for the CRM push recovery scan (ADR-0043 §2)."""
    try:
        await crm_push_recovery_scan()
    except Exception as exc:
        log.error(
            "crm_push_recovery_unhandled",
            detail=str(exc)[:300],
            exc_info=True,
        )


async def _safe_crm_status_dispatch() -> None:
    """Wrapper for the CRM mailbox-status dispatcher (ADR-0043 §2)."""
    try:
        await crm_status_dispatch()
    except Exception as exc:
        log.error(
            "crm_status_dispatch_unhandled",
            detail=str(exc)[:300],
            exc_info=True,
        )


async def main() -> None:
    settings = get_settings()
    configure_logging(level=settings.LOG_LEVEL, service="worker")
    log.info("worker_starting")

    init_engine(role="worker")

    scheduler = AsyncIOScheduler(timezone="UTC")
    # IMPORTANT: do NOT pass ``next_run_time=None`` here. In APScheduler 3.x
    # an explicit ``None`` is the "paused" sentinel — the job is added but
    # never scheduled to run. The "use the trigger" sentinel is the
    # internal ``undefined``, activated by simply NOT passing the kwarg.
    # See round-7 rework notes / Bug A.
    scheduler.add_job(
        _safe_sync_cycle,
        trigger=IntervalTrigger(minutes=settings.SYNC_INTERVAL_MINUTES),
        id="sync_cycle",
        coalesce=True,
        max_instances=1,
        misfire_grace_time=60,
    )
    scheduler.add_job(
        _safe_force_sync_dispatch,
        trigger=IntervalTrigger(seconds=10),
        id="force_sync_dispatcher",
        coalesce=True,
        max_instances=1,
        misfire_grace_time=10,
    )
    scheduler.add_job(
        _safe_cleanup,
        trigger=CronTrigger(hour=3, minute=0, timezone="UTC"),
        id="retention_cleanup",
        max_instances=1,
        misfire_grace_time=3600,
    )
    scheduler.add_job(
        _touch_alive,
        trigger=IntervalTrigger(seconds=30),
        id="alive_touch",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=10,
    )
    # ADR-0043 §2: CRM ingest-push dispatcher + recovery scan. Registered ONLY
    # when the CRM push is configured (``CRM_INGEST_URL`` + ``CRM_PUSH_SECRET``)
    # — a pre-cut-over deployment runs unchanged (jobs absent, sync_cycle does
    # not enqueue).
    if settings.crm_push_enabled:
        scheduler.add_job(
            _safe_crm_push_dispatch,
            trigger=IntervalTrigger(seconds=settings.CRM_PUSH_DISPATCH_INTERVAL_SECONDS),
            id="crm_push_dispatch",
            coalesce=True,
            max_instances=1,
            misfire_grace_time=30,
        )
        scheduler.add_job(
            _safe_crm_push_recovery,
            trigger=IntervalTrigger(seconds=settings.CRM_PUSH_RECOVERY_INTERVAL_SECONDS),
            id="crm_push_recovery",
            coalesce=True,
            max_instances=1,
            misfire_grace_time=600,
        )
    # ADR-0043 §2: CRM mailbox-status dispatcher. Registered ONLY when the
    # status channel is configured (``CRM_MAILBOX_STATUS_URL`` +
    # ``CRM_PUSH_SECRET``).
    if settings.crm_status_enabled:
        scheduler.add_job(
            _safe_crm_status_dispatch,
            trigger=IntervalTrigger(seconds=settings.CRM_STATUS_DISPATCH_INTERVAL_SECONDS),
            id="crm_status_dispatch",
            coalesce=True,
            max_instances=1,
            misfire_grace_time=30,
        )

    # Touch once immediately so the health-check works even before the first
    # 30-second tick.
    _touch_alive()

    scheduler.start()
    log.info(
        "worker_started",
        sync_interval_minutes=settings.SYNC_INTERVAL_MINUTES,
        force_sync_dispatch_seconds=10,
        max_concurrent_imap=settings.MAX_CONCURRENT_IMAP,
        crm_push_enabled=settings.crm_push_enabled,
        crm_status_enabled=settings.crm_status_enabled,
    )

    # Optional: kick off one sync immediately on boot, so we don't wait the
    # full interval before the first cycle. We hold a strong ref to keep
    # the task from being GC'd while it runs.
    initial_task = asyncio.create_task(_safe_sync_cycle(), name="initial_sync_cycle")

    # Wait for SIGTERM/SIGINT.
    stop_event = asyncio.Event()

    def _signal_handler(*_args: object) -> None:
        log.info("worker_signal_received")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            # Windows doesn't support add_signal_handler in a portable way.
            signal.signal(sig, lambda *_a: _signal_handler())

    await stop_event.wait()
    log.info("worker_stopping")
    scheduler.shutdown(wait=True)
    # Drain initial_task so we don't lose its result/exception on shutdown.
    if not initial_task.done():
        with contextlib.suppress(Exception):
            await asyncio.wait_for(initial_task, timeout=5)
    await dispose_engine()
    await close_redis()
    log.info("worker_stopped")


def _entrypoint() -> None:
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    _entrypoint()
