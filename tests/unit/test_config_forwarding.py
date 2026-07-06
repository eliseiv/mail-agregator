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


class TestForwardRelayDefaults:
    """ADR-0034 §5 — service SMTP relay settings + ``forward_relay_enabled``."""

    def test_relay_defaults(self) -> None:
        s = _settings()
        assert s.FORWARD_SMTP_HOST == ""
        assert s.FORWARD_SMTP_PORT == 587
        assert s.FORWARD_SMTP_USERNAME == ""
        assert s.FORWARD_SMTP_PASSWORD == ""
        assert s.FORWARD_SMTP_FROM == ""
        assert s.FORWARD_SMTP_STARTTLS is True
        assert s.FORWARD_SMTP_SSL is False

    def test_relay_disabled_by_default(self) -> None:
        assert _settings().forward_relay_enabled is False

    @pytest.mark.parametrize(
        "overrides",
        [
            {"FORWARD_SMTP_HOST": "relay.example"},  # from + username missing
            {"FORWARD_SMTP_HOST": "relay.example", "FORWARD_SMTP_FROM": "r@x"},  # username missing
            {"FORWARD_SMTP_HOST": "relay.example", "FORWARD_SMTP_USERNAME": "u"},  # from missing
            {"FORWARD_SMTP_FROM": "r@x", "FORWARD_SMTP_USERNAME": "u"},  # host missing
        ],
    )
    def test_relay_stays_off_when_any_required_field_missing(
        self, overrides: dict[str, str]
    ) -> None:
        assert _settings(**overrides).forward_relay_enabled is False

    def test_relay_enabled_when_host_from_username_set(self) -> None:
        s = _settings(
            FORWARD_SMTP_HOST="relay.example",
            FORWARD_SMTP_FROM="relay@service.example",
            FORWARD_SMTP_USERNAME="relay-user",
        )
        assert s.forward_relay_enabled is True

    @pytest.mark.parametrize("value", ["0", "65536"])
    def test_relay_port_out_of_range_rejected(self, value: str) -> None:
        with pytest.raises(ValidationError):
            _settings(FORWARD_SMTP_PORT=value)
