"""Worker entrypoint: APScheduler with sync_cycle + retention_cleanup.

Started by ``deploy/Dockerfile`` (target=worker): ``python -m worker.app.main``.

Jobs (ADR-0003):
- ``sync_cycle`` — every ``SYNC_INTERVAL_MINUTES`` (default 5). Polls all
  active mailboxes.
- ``force_sync_dispatcher`` — every 10s. Drains ``force_sync:{id}`` Redis
  markers written by ``POST /accounts/{id}/sync`` so the "Sync now" UI
  button delivers sub-10-second latency without lowering the regular
  poll cadence.
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
from shared.storage import get_storage
from worker.app.cleanup import retention_cleanup
from worker.app.sync_cycle import force_sync_dispatch, sync_cycle
from worker.app.tg_notify_dispatch import tg_notify_dispatch
from worker.app.tg_notify_recovery import tg_notify_recovery_scan

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


async def _safe_tg_notify_dispatch() -> None:
    """Wrapper for the Telegram notification dispatcher (ADR-0022 §2.4)."""
    try:
        await tg_notify_dispatch()
    except Exception as exc:
        log.error(
            "tg_notify_dispatch_unhandled",
            detail=str(exc)[:300],
            exc_info=True,
        )


async def _safe_tg_notify_recovery() -> None:
    """Wrapper for the Telegram notification recovery scan (ADR-0022 §2.8)."""
    try:
        await tg_notify_recovery_scan()
    except Exception as exc:
        log.error(
            "tg_notify_recovery_unhandled",
            detail=str(exc)[:300],
            exc_info=True,
        )


async def _bootstrap() -> None:
    """One-time async startup: ensure bucket etc."""
    try:
        await get_storage().ensure_bucket()
    except Exception as exc:
        log.warning("worker_ensure_bucket_failed", detail=str(exc)[:200])


async def main() -> None:
    settings = get_settings()
    configure_logging(level=settings.LOG_LEVEL, service="worker")
    log.info("worker_starting")

    init_engine(role="worker")
    await _bootstrap()

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
    # ADR-0022 §2.4: Telegram notification dispatcher (drains Redis queue).
    scheduler.add_job(
        _safe_tg_notify_dispatch,
        trigger=IntervalTrigger(seconds=settings.TG_NOTIFY_DISPATCH_INTERVAL_SECONDS),
        id="tg_notify_dispatch",
        coalesce=True,
        max_instances=1,
        misfire_grace_time=30,
    )
    # ADR-0022 §2.8: recovery scan (re-enqueues lost notifications).
    scheduler.add_job(
        _safe_tg_notify_recovery,
        trigger=IntervalTrigger(seconds=settings.TG_NOTIFY_RECOVERY_INTERVAL_SECONDS),
        id="tg_notify_recovery",
        coalesce=True,
        max_instances=1,
        misfire_grace_time=600,
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
        tg_notify_dispatch_seconds=settings.TG_NOTIFY_DISPATCH_INTERVAL_SECONDS,
        tg_notify_recovery_seconds=settings.TG_NOTIFY_RECOVERY_INTERVAL_SECONDS,
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
