"""ADR-0026 update §2 — transient last_sync_error suppression window.

Scope D of the QA task. :func:`sync_cycle._should_suppress_transient` decides
whether a TRANSIENT ``last_sync_error`` write is suppressed (hidden from the UI)
because the mailbox synced successfully recently:

* ``last_synced_at`` within ``SYNC_TRANSIENT_SUPPRESS_MINUTES`` -> True (suppress
  the sporadic flake; the next cycle retries / succeeds).
* ``last_synced_at`` older than the window (stale) -> False (the sync is
  genuinely stuck; the operator must see the error).
* ``last_synced_at is None`` (never synced) -> False.
* ``SYNC_TRANSIENT_SUPPRESS_MINUTES == 0`` -> suppression disabled -> False.
* a naive (tz-unaware) ``last_synced_at`` must not raise (coerced to UTC).

PURE unit scope: ``get_settings`` is monkeypatched so the window is explicit and
no .env / DB is touched.
"""

from __future__ import annotations

import datetime as _dt

import pytest

from worker.app import sync_cycle as sc


def _set_window(monkeypatch: pytest.MonkeyPatch, minutes: int) -> None:
    """Force SYNC_TRANSIENT_SUPPRESS_MINUTES regardless of env/.env."""

    class _S:
        SYNC_TRANSIENT_SUPPRESS_MINUTES = minutes

    monkeypatch.setattr(sc, "get_settings", lambda: _S())


_NOW = _dt.datetime.now(_dt.UTC)


class TestShouldSuppressTransient:
    def test_fresh_last_synced_within_window_suppresses(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_window(monkeypatch, 60)
        fresh = _NOW - _dt.timedelta(minutes=5)
        assert sc._should_suppress_transient(fresh) is True

    def test_just_inside_window_boundary_suppresses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Boundary: age <= window suppresses (inclusive)."""
        _set_window(monkeypatch, 60)
        edge = _dt.datetime.now(_dt.UTC) - _dt.timedelta(minutes=59, seconds=58)
        assert sc._should_suppress_transient(edge) is True

    def test_stale_last_synced_does_not_suppress(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _set_window(monkeypatch, 60)
        stale = _NOW - _dt.timedelta(minutes=600)
        assert sc._should_suppress_transient(stale) is False

    def test_none_last_synced_does_not_suppress(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Never-synced account -> the sync is stuck -> write the error."""
        _set_window(monkeypatch, 60)
        assert sc._should_suppress_transient(None) is False

    def test_zero_window_disables_suppression(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Window == 0 -> pre-update behaviour: every transient error is written."""
        _set_window(monkeypatch, 0)
        fresh = _NOW - _dt.timedelta(minutes=1)
        assert sc._should_suppress_transient(fresh) is False
        assert sc._should_suppress_transient(None) is False

    def test_naive_fresh_datetime_is_safe_and_suppresses(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A naive (tz-unaware) timestamp must be coerced to UTC, never raise."""
        _set_window(monkeypatch, 60)
        naive_fresh = (_dt.datetime.now(_dt.UTC) - _dt.timedelta(minutes=5)).replace(tzinfo=None)
        assert sc._should_suppress_transient(naive_fresh) is True

    def test_naive_stale_datetime_is_safe_and_does_not_suppress(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_window(monkeypatch, 60)
        naive_stale = (_dt.datetime.now(_dt.UTC) - _dt.timedelta(hours=10)).replace(tzinfo=None)
        assert sc._should_suppress_transient(naive_stale) is False

    def test_default_window_is_sixty_minutes(self) -> None:
        """Pin the documented default (ADR-0026 update §2)."""
        from shared.config import get_settings

        get_settings.cache_clear()
        assert get_settings().SYNC_TRANSIENT_SUPPRESS_MINUTES == 60
