"""Unit tests for ADR-0027 §4/§7 parametrisation of ``send_notification``.

Source of truth: ``backend/app/telegram/bot.py``.

We mock the network at the ``httpx.AsyncClient`` boundary (the only external
border) so no real request leaves the process; the fake records the POST URL
and JSON body so we can assert the token embedded in the URL and the presence
/ absence of the inline-button ``reply_markup``.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from backend.app.telegram import bot as bot_mod
from shared.config import get_settings

pytestmark = pytest.mark.unit


class _Recorder:
    """Captures the single sendMessage POST made by ``send_notification``."""

    def __init__(self, status: int = 200, json_body: dict[str, Any] | None = None) -> None:
        self.status = status
        self.json_body = json_body or {"ok": True, "result": {"message_id": 4242}}
        self.url: str | None = None
        self.payload: dict[str, Any] | None = None


def _install_fake_client(monkeypatch: pytest.MonkeyPatch, rec: _Recorder) -> None:
    """Replace ``httpx.AsyncClient`` in bot.py with a fake recording client."""

    class _FakeResponse:
        status_code = rec.status

        def json(self) -> dict[str, Any]:
            return rec.json_body

        @property
        def text(self) -> str:
            return "fake"

        @property
        def headers(self) -> dict[str, str]:
            return {}

    class _FakeClient:
        def __init__(self, *_a: Any, **_kw: Any) -> None:
            pass

        async def __aenter__(self) -> _FakeClient:
            return self

        async def __aexit__(self, *_a: Any) -> None:
            return None

        async def post(self, url: str, *, json: dict[str, Any]) -> _FakeResponse:
            rec.url = url
            rec.payload = json
            return _FakeResponse()

    monkeypatch.setattr(bot_mod.httpx, "AsyncClient", _FakeClient)


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> Any:
    """Each test pins its own env; clear the lru-cache before and after."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Push bot path: explicit bot_token + with_button=False
# ---------------------------------------------------------------------------


class TestPushBotPath:
    async def test_explicit_token_in_url_and_no_button(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Main bot deliberately DISABLED (empty BOT_TOKEN, no webhook secret)
        # to prove the push path bypasses the telegram_bot_enabled guard.
        monkeypatch.setenv("BOT_TOKEN", "")
        monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "")
        monkeypatch.setenv("TELEGRAM_WEBAPP_URL", "")
        get_settings.cache_clear()
        assert get_settings().telegram_bot_enabled is False

        rec = _Recorder()
        _install_fake_client(monkeypatch, rec)

        result = await bot_mod.send_notification(
            chat_id=999,
            text_html="<b>hi</b>",
            message_id=7,
            bot_token="IVANTOKEN",
            with_button=False,
        )

        # Guard bypassed even though the main bot is disabled.
        assert result.kind == "ok"
        assert result.telegram_message_id == 4242
        # URL carries the push-bot token, not BOT_TOKEN.
        assert rec.url is not None and "botIVANTOKEN/" in rec.url
        # No inline button for push bots (callback would hang — ADR-0027 §7).
        assert rec.payload is not None
        assert "reply_markup" not in rec.payload
        assert rec.payload["parse_mode"] == "HTML"
        assert rec.payload["chat_id"] == 999

    async def test_push_token_used_even_when_main_token_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BOT_TOKEN", "MAINTOKEN")
        get_settings.cache_clear()

        rec = _Recorder()
        _install_fake_client(monkeypatch, rec)

        await bot_mod.send_notification(
            chat_id=1,
            text_html="x",
            message_id=2,
            bot_token="PUSHTOKEN",
            with_button=False,
        )
        assert rec.url is not None
        assert "botPUSHTOKEN/" in rec.url
        assert "botMAINTOKEN/" not in rec.url


# ---------------------------------------------------------------------------
# Default (main bot) path: bot_token=None — ADR-0022 behaviour preserved
# ---------------------------------------------------------------------------


class TestMainBotPathUnchanged:
    async def test_default_uses_bot_token_and_keeps_button(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Fully configure the main bot so the guard passes.
        monkeypatch.setenv("BOT_TOKEN", "MAINTOKEN")
        monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "secret")
        monkeypatch.setenv("TELEGRAM_WEBAPP_URL", "https://app.example.com")
        get_settings.cache_clear()
        assert get_settings().telegram_bot_enabled is True

        rec = _Recorder()
        _install_fake_client(monkeypatch, rec)

        result = await bot_mod.send_notification(
            chat_id=5,
            text_html="hello",
            message_id=42,
        )
        assert result.kind == "ok"
        # Default token = BOT_TOKEN.
        assert rec.url is not None and "botMAINTOKEN/" in rec.url
        # Button present by default (ADR-0022 behaviour unchanged).
        assert rec.payload is not None
        markup = rec.payload.get("reply_markup")
        assert markup is not None
        button = markup["inline_keyboard"][0][0]
        assert button["callback_data"] == "msg:42"

    async def test_default_guard_disables_when_main_bot_off(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Main bot disabled + no explicit token -> guard returns "disabled"
        # WITHOUT touching the network.
        monkeypatch.setenv("BOT_TOKEN", "")
        monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "")
        monkeypatch.setenv("TELEGRAM_WEBAPP_URL", "")
        get_settings.cache_clear()
        assert get_settings().telegram_bot_enabled is False

        rec = _Recorder()
        _install_fake_client(monkeypatch, rec)

        result = await bot_mod.send_notification(
            chat_id=5,
            text_html="hello",
            message_id=42,
        )
        assert result.kind == "disabled"
        # No POST happened.
        assert rec.url is None
        assert rec.payload is None


# ---------------------------------------------------------------------------
# Bot API outcome mapping still works on the push (token) path
# ---------------------------------------------------------------------------


class TestPushOutcomeMapping:
    async def test_403_maps_to_dead(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BOT_TOKEN", "")
        get_settings.cache_clear()
        rec = _Recorder(
            status=403,
            json_body={"ok": False, "description": "Forbidden: bot was blocked by the user"},
        )
        _install_fake_client(monkeypatch, rec)
        result = await bot_mod.send_notification(
            chat_id=1, text_html="x", message_id=1, bot_token="T", with_button=False
        )
        assert result.kind == "dead"

    async def test_429_maps_to_retry_after(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BOT_TOKEN", "")
        get_settings.cache_clear()
        rec = _Recorder(
            status=429,
            json_body={"ok": False, "parameters": {"retry_after": 7}},
        )
        _install_fake_client(monkeypatch, rec)
        result = await bot_mod.send_notification(
            chat_id=1, text_html="x", message_id=1, bot_token="T", with_button=False
        )
        assert result.kind == "retry_after"
        assert result.retry_after_sec == 7

    async def test_network_error_maps_to_transient(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BOT_TOKEN", "")
        get_settings.cache_clear()

        class _BoomClient:
            def __init__(self, *_a: Any, **_kw: Any) -> None:
                pass

            async def __aenter__(self) -> _BoomClient:
                return self

            async def __aexit__(self, *_a: Any) -> None:
                return None

            async def post(self, *_a: Any, **_kw: Any) -> Any:
                raise httpx.ConnectError("boom")

        monkeypatch.setattr(bot_mod.httpx, "AsyncClient", _BoomClient)
        result = await bot_mod.send_notification(
            chat_id=1, text_html="x", message_id=1, bot_token="T", with_button=False
        )
        assert result.kind == "transient"
