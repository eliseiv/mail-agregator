"""ADR-0027 §8 — push-bot tokens are redacted in structured logs.

Source of truth: ``shared/logging.py`` (``REDACT_KEYS`` + ``_redact_processor``).

A bug-token leak lets an attacker impersonate the bot to the admins, so the
three new token env keys must be masked exactly like ``BOT_TOKEN`` whenever a
settings dump accidentally feeds them through a structlog event.
"""

from __future__ import annotations

import pytest

from shared.logging import REDACT_KEYS, _redact_processor

pytestmark = pytest.mark.unit

_PUSH_TOKEN_KEYS = ["BOT_IVAN_TOKEN", "BOT_ALEXANDRA_TOKEN", "BOT_ANDREI_TOKEN"]


class TestPushBotTokenRedaction:
    @pytest.mark.parametrize("key", _PUSH_TOKEN_KEYS)
    def test_key_is_in_redact_list(self, key: str) -> None:
        assert key in REDACT_KEYS

    @pytest.mark.parametrize("key", _PUSH_TOKEN_KEYS)
    def test_value_is_masked_by_processor(self, key: str) -> None:
        event = {"event": "settings_dump", key: "123456:SUPER_SECRET_BOT_TOKEN"}
        out = _redact_processor(None, "info", event)
        assert out[key] == "[REDACTED]"
        assert "SUPER_SECRET_BOT_TOKEN" not in str(out)

    def test_full_settings_dump_masks_all_three_tokens(self) -> None:
        event = {
            "event": "settings_dump",
            "BOT_IVAN_TOKEN": "ivan-secret",
            "BOT_ALEXANDRA_TOKEN": "alexandra-secret",
            "BOT_ANDREI_TOKEN": "andrei-secret",
            "ADMIN_TELEGRAM_IDS": "111,222",  # not a secret — stays visible
        }
        out = _redact_processor(None, "info", event)
        assert out["BOT_IVAN_TOKEN"] == "[REDACTED]"
        assert out["BOT_ALEXANDRA_TOKEN"] == "[REDACTED]"
        assert out["BOT_ANDREI_TOKEN"] == "[REDACTED]"
        # Chat ids are not secret; left as-is.
        assert out["ADMIN_TELEGRAM_IDS"] == "111,222"
        rendered = str(out)
        for secret in ("ivan-secret", "alexandra-secret", "andrei-secret"):
            assert secret not in rendered


# ---------------------------------------------------------------------------
# round-42 (ADR-0027 §8) — per-bot push-webhook secrets must also be redacted.
# A leaked webhook secret lets an attacker forge push-webhook updates (the
# header X-Telegram-Bot-Api-Secret-Token is the only proof for that route).
# ---------------------------------------------------------------------------

_PUSH_SECRET_KEYS = [
    "BOT_IVAN_WEBHOOK_SECRET",
    "BOT_ALEXANDRA_WEBHOOK_SECRET",
    "BOT_ANDREI_WEBHOOK_SECRET",
]


class TestPushBotWebhookSecretRedaction:
    @pytest.mark.parametrize("key", _PUSH_SECRET_KEYS)
    def test_key_is_in_redact_list(self, key: str) -> None:
        assert key in REDACT_KEYS

    @pytest.mark.parametrize("key", _PUSH_SECRET_KEYS)
    def test_value_is_masked_by_processor(self, key: str) -> None:
        event = {"event": "settings_dump", key: "deadbeefdeadbeefdeadbeefdeadbeef"}
        out = _redact_processor(None, "info", event)
        assert out[key] == "[REDACTED]"
        assert "deadbeef" not in str(out)

    def test_full_settings_dump_masks_all_three_secrets(self) -> None:
        event = {
            "event": "settings_dump",
            "BOT_IVAN_WEBHOOK_SECRET": "ivanhook",
            "BOT_ALEXANDRA_WEBHOOK_SECRET": "alexhook",
            "BOT_ANDREI_WEBHOOK_SECRET": "andreihook",
            # The header form (as it might appear on an inbound request log).
            "X-Telegram-Bot-Api-Secret-Token": "ivanhook",
        }
        out = _redact_processor(None, "info", event)
        assert out["BOT_IVAN_WEBHOOK_SECRET"] == "[REDACTED]"
        assert out["BOT_ALEXANDRA_WEBHOOK_SECRET"] == "[REDACTED]"
        assert out["BOT_ANDREI_WEBHOOK_SECRET"] == "[REDACTED]"
        assert out["X-Telegram-Bot-Api-Secret-Token"] == "[REDACTED]"
        rendered = str(out)
        for secret in ("ivanhook", "alexhook", "andreihook"):
            assert secret not in rendered
