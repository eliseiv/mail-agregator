"""Unit tests for the ADR-0034 forwarding settings (``shared/config.py`` §6).

Asserts the documented defaults and the pydantic field bounds so a regression
in the kill-switch / batch / budget knobs is caught at unit level (no env).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from shared.config import Settings

pytestmark = pytest.mark.unit

# Minimal required env so ``Settings`` constructs without a real .env.
_BASE_ENV = {
    "DATABASE_URL": "postgresql+asyncpg://u:p@localhost:5432/db",
    "REDIS_URL": "redis://localhost:6379/0",
    "S3_ENDPOINT_URL": "http://localhost:9000",
    "S3_ACCESS_KEY": "k",
    "S3_SECRET_KEY": "s",
    "MAIL_ENCRYPTION_KEY": "HSoYMcwRZLguwQpz+kHPwifN9LvO/H86royMLyRgclo=",
    "ADMIN_LOGIN": "admin",
    "ADMIN_PASSWORD": "pw",
}


def _settings(**overrides: str) -> Settings:
    # ``_env_file`` is a pydantic-settings init kwarg (not a model field).
    return Settings(_env_file=None, **{**_BASE_ENV, **overrides})  # type: ignore[call-arg, arg-type]


class TestForwardingDefaults:
    def test_defaults(self) -> None:
        s = _settings()
        assert s.FORWARDING_ENABLED is True
        assert s.FORWARD_DISPATCH_INTERVAL_SECONDS == 5
        assert s.FORWARD_BATCH_SIZE == 30
        assert s.FORWARD_MAX_TOTAL_BYTES == 26_214_400
        assert s.FORWARD_PER_ACCOUNT_PER_MINUTE == 30


class TestForwardingBounds:
    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("FORWARD_DISPATCH_INTERVAL_SECONDS", "0"),
            ("FORWARD_DISPATCH_INTERVAL_SECONDS", "601"),
            ("FORWARD_BATCH_SIZE", "0"),
            ("FORWARD_BATCH_SIZE", "501"),
            ("FORWARD_MAX_TOTAL_BYTES", "1023"),
            ("FORWARD_PER_ACCOUNT_PER_MINUTE", "0"),
        ],
    )
    def test_out_of_range_rejected(self, field: str, value: str) -> None:
        with pytest.raises(ValidationError):
            _settings(**{field: value})

    def test_kill_switch_can_be_disabled(self) -> None:
        assert _settings(FORWARDING_ENABLED="false").FORWARDING_ENABLED is False
