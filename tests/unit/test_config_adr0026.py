"""Unit tests for ADR-0026 config knobs (shared/config.py).

Scope F: defaults + Field boundary validation for the four ADR-0026 settings.
We construct ``Settings`` directly with the required secrets so the model
validator passes, and assert defaults / bounds via pydantic ValidationError.
"""

from __future__ import annotations

import base64

import pytest
from pydantic import ValidationError

from shared.config import Settings

_REQUIRED = {
    "MAIL_ENCRYPTION_KEY": base64.b64encode(b"x" * 32).decode(),
    "APP_ENV": "dev",
}


def _make(**overrides: object) -> Settings:
    return Settings(_env_file=None, **{**_REQUIRED, **overrides})  # type: ignore[arg-type]


class TestDefaults:
    def test_defaults(self) -> None:
        s = _make()
        assert s.SYNC_MAX_CONSECUTIVE_FAILURES == 3
        assert s.SYNC_MASS_FAILURE_RATIO == 0.5
        assert s.SYNC_MASS_FAILURE_MIN == 5
        # ADR-0026 update §4: default bumped 2 -> 3 so the connect/login retry
        # loop covers the sporadic Outlook "authenticated but not connected"
        # flake out of the box (0.5/1.0/2.0 backoff).
        assert s.SYNC_CONNECT_RETRIES == 3
        # ADR-0026 update §2: transient last_sync_error suppression window.
        assert s.SYNC_TRANSIENT_SUPPRESS_MINUTES == 60


class TestBoundaries:
    @pytest.mark.parametrize("val", [1, 20])
    def test_max_consecutive_failures_in_range_ok(self, val: int) -> None:
        assert val == _make(SYNC_MAX_CONSECUTIVE_FAILURES=val).SYNC_MAX_CONSECUTIVE_FAILURES

    @pytest.mark.parametrize("val", [0, 21])
    def test_max_consecutive_failures_out_of_range_rejected(self, val: int) -> None:
        with pytest.raises(ValidationError):
            _make(SYNC_MAX_CONSECUTIVE_FAILURES=val)

    @pytest.mark.parametrize("val", [0.0, 1.0])
    def test_mass_failure_ratio_in_range_ok(self, val: float) -> None:
        assert val == _make(SYNC_MASS_FAILURE_RATIO=val).SYNC_MASS_FAILURE_RATIO

    @pytest.mark.parametrize("val", [-0.1, 1.1])
    def test_mass_failure_ratio_out_of_range_rejected(self, val: float) -> None:
        with pytest.raises(ValidationError):
            _make(SYNC_MASS_FAILURE_RATIO=val)

    @pytest.mark.parametrize("val", [1, 10_000])
    def test_mass_failure_min_in_range_ok(self, val: int) -> None:
        assert val == _make(SYNC_MASS_FAILURE_MIN=val).SYNC_MASS_FAILURE_MIN

    @pytest.mark.parametrize("val", [0, 10_001])
    def test_mass_failure_min_out_of_range_rejected(self, val: int) -> None:
        with pytest.raises(ValidationError):
            _make(SYNC_MASS_FAILURE_MIN=val)

    @pytest.mark.parametrize("val", [0, 10])
    def test_connect_retries_in_range_ok(self, val: int) -> None:
        assert val == _make(SYNC_CONNECT_RETRIES=val).SYNC_CONNECT_RETRIES

    @pytest.mark.parametrize("val", [-1, 11])
    def test_connect_retries_out_of_range_rejected(self, val: int) -> None:
        with pytest.raises(ValidationError):
            _make(SYNC_CONNECT_RETRIES=val)

    @pytest.mark.parametrize("val", [0, 60, 10_080])
    def test_transient_suppress_minutes_in_range_ok(self, val: int) -> None:
        assert val == _make(SYNC_TRANSIENT_SUPPRESS_MINUTES=val).SYNC_TRANSIENT_SUPPRESS_MINUTES

    @pytest.mark.parametrize("val", [-1, 10_081])
    def test_transient_suppress_minutes_out_of_range_rejected(self, val: int) -> None:
        with pytest.raises(ValidationError):
            _make(SYNC_TRANSIENT_SUPPRESS_MINUTES=val)
