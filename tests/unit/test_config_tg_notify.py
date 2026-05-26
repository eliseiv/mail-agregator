"""Unit tests for round-31 (ADR-0022 §2.1 / §2.9) config additions:
``TG_NOTIFY_ALL_MESSAGES`` and ``TG_SEND_PER_CHAT_PER_MINUTE``.

Source of truth: ``shared/config.py``.

We instantiate :class:`Settings` directly with explicit keyword arguments.
pydantic-settings treats init kwargs as the highest-priority source, so the
ambient ``.env`` does not interfere with the value under test, while the
required secrets (MAIL_ENCRYPTION_KEY etc.) are supplied inline so the
``model_validator`` passes.
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


class TestDefaults:
    def test_tg_notify_all_messages_defaults_true(self) -> None:
        s = _settings()
        assert s.TG_NOTIFY_ALL_MESSAGES is True

    def test_tg_send_per_chat_defaults_20(self) -> None:
        s = _settings()
        assert s.TG_SEND_PER_CHAT_PER_MINUTE == 20


class TestTgNotifyAllMessagesParsing:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("true", True),
            ("True", True),
            ("1", True),
            ("false", False),
            ("False", False),
            ("0", False),
            (True, True),
            (False, False),
        ],
    )
    def test_bool_parsing(self, raw: object, expected: bool) -> None:
        s = _settings(TG_NOTIFY_ALL_MESSAGES=raw)
        assert s.TG_NOTIFY_ALL_MESSAGES is expected


class TestTgSendPerChatBounds:
    def test_lower_bound_ge_1_accepts_1(self) -> None:
        assert _settings(TG_SEND_PER_CHAT_PER_MINUTE=1).TG_SEND_PER_CHAT_PER_MINUTE == 1

    def test_upper_bound_le_60_accepts_60(self) -> None:
        assert _settings(TG_SEND_PER_CHAT_PER_MINUTE=60).TG_SEND_PER_CHAT_PER_MINUTE == 60

    def test_below_lower_bound_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _settings(TG_SEND_PER_CHAT_PER_MINUTE=0)

    def test_above_upper_bound_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _settings(TG_SEND_PER_CHAT_PER_MINUTE=61)

    def test_integer_parsing_from_string(self) -> None:
        # Env always arrives as a string; pydantic coerces to int.
        assert _settings(TG_SEND_PER_CHAT_PER_MINUTE="42").TG_SEND_PER_CHAT_PER_MINUTE == 42
