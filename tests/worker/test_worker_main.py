"""Unit tests for ``worker.app.main`` — the standalone job functions.

The full ``main()`` runs ``asyncio.run`` and blocks on ``stop_event.wait()``,
so we don't exercise it end-to-end. We *do* exercise:

- ``_touch_alive``: filesystem touch + permission-error path.
- ``_safe_sync_cycle`` / ``_safe_cleanup``: never re-raise; log on failure.
- ``_bootstrap``: never re-raise; logs warning on bucket failure.
- ``_entrypoint``: handles KeyboardInterrupt gracefully.

Source of truth: ``worker/app/main.py``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from worker.app import main as worker_main

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# _touch_alive
# ---------------------------------------------------------------------------


class TestTouchAlive:
    def test_touch_creates_file_and_updates_mtime(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "alive_subdir" / "worker_alive"
        monkeypatch.setattr(worker_main, "ALIVE_FILE", target)
        # Should create the parent dir + the file.
        worker_main._touch_alive()
        assert target.exists()
        first_mtime = target.stat().st_mtime
        # Touching again updates mtime (sleep 0 to keep test fast — same
        # second is fine).
        worker_main._touch_alive()
        assert target.stat().st_mtime >= first_mtime

    def test_touch_swallows_oserror(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If the file system raises (e.g. read-only mount), the worker
        must not crash — only log a warning.
        """

        class _BoomPath:
            def __init__(self) -> None:
                self.parent = self

            def mkdir(self, *_a: Any, **_kw: Any) -> None:
                raise OSError("read-only filesystem")

            def touch(self, *_a: Any, **_kw: Any) -> None:
                raise OSError("read-only filesystem")

        monkeypatch.setattr(worker_main, "ALIVE_FILE", _BoomPath())
        # Must not raise.
        worker_main._touch_alive()


# ---------------------------------------------------------------------------
# _safe_sync_cycle
# ---------------------------------------------------------------------------


class TestSafeSyncCycle:
    @pytest.mark.asyncio
    async def test_propagates_nothing_on_success(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        called: list[bool] = []

        async def _ok() -> None:
            called.append(True)

        monkeypatch.setattr(worker_main, "sync_cycle", _ok)
        await worker_main._safe_sync_cycle()
        assert called == [True]

    @pytest.mark.asyncio
    async def test_swallows_exception(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _boom() -> None:
            raise RuntimeError("kaboom")

        monkeypatch.setattr(worker_main, "sync_cycle", _boom)
        # Must not propagate.
        await worker_main._safe_sync_cycle()


# ---------------------------------------------------------------------------
# _safe_cleanup
# ---------------------------------------------------------------------------


class TestSafeCleanup:
    @pytest.mark.asyncio
    async def test_swallows_exception(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _boom() -> None:
            raise RuntimeError("retention boom")

        monkeypatch.setattr(worker_main, "retention_cleanup", _boom)
        await worker_main._safe_cleanup()  # — must not raise

    @pytest.mark.asyncio
    async def test_succeeds_quietly(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        called: list[bool] = []

        async def _ok() -> None:
            called.append(True)

        monkeypatch.setattr(worker_main, "retention_cleanup", _ok)
        await worker_main._safe_cleanup()
        assert called == [True]


# ---------------------------------------------------------------------------
# _bootstrap
# ---------------------------------------------------------------------------


class TestBootstrap:
    @pytest.mark.asyncio
    async def test_swallows_bucket_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _BadStorage:
            async def ensure_bucket(self) -> None:
                raise RuntimeError("minio down")

        monkeypatch.setattr(worker_main, "get_storage", lambda: _BadStorage())
        await worker_main._bootstrap()  # — must not raise

    @pytest.mark.asyncio
    async def test_calls_ensure_bucket(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        called: list[bool] = []

        class _OkStorage:
            async def ensure_bucket(self) -> None:
                called.append(True)

        monkeypatch.setattr(worker_main, "get_storage", lambda: _OkStorage())
        await worker_main._bootstrap()
        assert called == [True]


# ---------------------------------------------------------------------------
# _entrypoint — KeyboardInterrupt path
# ---------------------------------------------------------------------------


class TestEntrypoint:
    def test_keyboard_interrupt_exits_zero(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _boom() -> None:
            raise KeyboardInterrupt()

        # Replace ``main`` with our ctrl-c emitter.
        monkeypatch.setattr(worker_main, "main", _boom)
        with pytest.raises(SystemExit) as ei:
            worker_main._entrypoint()
        assert ei.value.code == 0

    def test_other_exception_propagates(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Non-KeyboardInterrupt errors should propagate so docker
        restarts the container.
        """

        async def _boom() -> None:
            raise RuntimeError("boot failure")

        monkeypatch.setattr(worker_main, "main", _boom)
        with pytest.raises(RuntimeError):
            worker_main._entrypoint()


# ---------------------------------------------------------------------------
# Module-level invariants
# ---------------------------------------------------------------------------


class TestInvariants:
    def test_alive_file_default_under_tmp(self) -> None:
        # Sanity: the docker healthcheck reads /tmp/worker_alive; the path
        # should not be moved by accident.
        assert str(worker_main.ALIVE_FILE).replace("\\", "/").endswith("/tmp/worker_alive")
