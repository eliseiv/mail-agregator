"""Unit tests for the ADR-0029 config addition
``EXTERNAL_API_RATE_LIMIT_PER_MINUTE`` (operator-tunable per-IP cap for
``GET /api/external/messages``).

Source of truth: ``shared/config.py``
(``EXTERNAL_API_RATE_LIMIT_PER_MINUTE: int = Field(default=120, ge=1, le=10000)``)
+ ``backend/app/external/router.py`` (consume-time override of
``LIMIT_EXTERNAL_API.capacity``).

We instantiate :class:`Settings` directly with explicit keyword arguments.
pydantic-settings treats init kwargs as the highest-priority source, so the
ambient ``.env`` does not interfere with the value under test, while the
required secrets (MAIL_ENCRYPTION_KEY etc.) are supplied inline so the
``model_validator`` passes. Mirrors ``tests/unit/test_config_tg_notify.py``.
"""

from __future__ import annotations

import base64

import pytest
from pydantic import ValidationError

from shared.config import Settings

pytestmark = pytest.mark.unit

# A valid base64 of exactly 32 bytes for MAIL_ENCRYPTION_KEY.
_VALID_KEY = base64.b64encode(b"\x00" * 32).decode()

# Minimal set of required-in-prod secrets so the model_validator passes.
_REQUIRED = {
    "MAIL_ENCRYPTION_KEY": _VALID_KEY,
    "ADMIN_PASSWORD": "x",
    "S3_ACCESS_KEY": "x",
    "S3_SECRET_KEY": "x",
}


def _settings(**overrides: object) -> Settings:
    return Settings(**{**_REQUIRED, **overrides})  # type: ignore[arg-type]


class TestDefault:
    def test_external_api_rate_limit_defaults_120(self) -> None:
        # ADR-0029 §1: 120 req / 60 s per IP is the documented default cap.
        assert _settings().EXTERNAL_API_RATE_LIMIT_PER_MINUTE == 120


class TestBounds:
    def test_lower_bound_ge_1_accepts_1(self) -> None:
        assert (
            _settings(EXTERNAL_API_RATE_LIMIT_PER_MINUTE=1).EXTERNAL_API_RATE_LIMIT_PER_MINUTE == 1
        )

    def test_zero_below_lower_bound_rejected(self) -> None:
        # ge=1 -> 0 is invalid (a 0 cap would block every request, so it is
        # disallowed rather than silently disabling the endpoint).
        with pytest.raises(ValidationError):
            _settings(EXTERNAL_API_RATE_LIMIT_PER_MINUTE=0)

    def test_negative_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _settings(EXTERNAL_API_RATE_LIMIT_PER_MINUTE=-1)

    def test_upper_bound_le_10000_accepts_10000(self) -> None:
        assert (
            _settings(EXTERNAL_API_RATE_LIMIT_PER_MINUTE=10000).EXTERNAL_API_RATE_LIMIT_PER_MINUTE
            == 10000
        )

    def test_above_upper_bound_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _settings(EXTERNAL_API_RATE_LIMIT_PER_MINUTE=10001)


class TestParsing:
    def test_integer_parsing_from_string(self) -> None:
        # Env always arrives as a string; pydantic coerces to int.
        assert (
            _settings(EXTERNAL_API_RATE_LIMIT_PER_MINUTE="42").EXTERNAL_API_RATE_LIMIT_PER_MINUTE
            == 42
        )

    def test_non_numeric_string_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _settings(EXTERNAL_API_RATE_LIMIT_PER_MINUTE="not-a-number")
