"""ADR-0027 §11 (round-42) — ``bot_token`` parametrisation of the two outbound
callback helpers used by ``handle_push_callback_query``.

Source of truth: ``backend/app/telegram/bot.py``
(``send_html_message`` / ``answer_callback_query`` / ``_post_send_message``).

The push-callback replies with THIS push-bot's token; the main launcher bot
path (``bot_token=None``) must stay byte-for-byte unchanged. We mock the
network at the ``httpx.AsyncClient`` boundary and assert the token embedded in
the POST URL.
"""

from __future__ import annotations

from typing import Any

import pytest

from backend.app.telegram import bot as bot_mod
from shared.config import get_settings

pytestmark = pytest.mark.unit


class _Recorder:
    def __init__(self) -> None:
        self.url: str | None = None
        self.payload: dict[str, Any] | None = None


def _install_fake_client(monkeypatch: pytest.MonkeyPatch, rec: _Recorder) -> None:
    class _FakeResponse:
        status_code = 200

        def json(self) -> dict[str, Any]:
            return {"ok": True, "result": {"message_id": 1}}

        @property
        def text(self) -> str:
            return "ok"

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
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# send_html_message
# ---------------------------------------------------------------------------


class TestSendHtmlMessageToken:
    async def test_explicit_bot_token_in_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BOT_TOKEN", "MAINTOKEN")
        get_settings.cache_clear()
        rec = _Recorder()
        _install_fake_client(monkeypatch, rec)

        await bot_mod.send_html_message(555, "<b>hi</b>", bot_token="IVANX")

        assert rec.url is not None
        assert "botIVANX/sendMessage" in rec.url
        assert "botMAINTOKEN/" not in rec.url
        assert rec.payload is not None
        assert rec.payload["parse_mode"] == "HTML"
        assert rec.payload["chat_id"] == 555
        assert rec.payload["disable_web_page_preview"] is True

    async def test_none_token_falls_back_to_main_bot_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # bot_token omitted (None) → the main BOT_TOKEN is used; the main bot
        # path is NOT broken by the round-42 parametrisation.
        monkeypatch.setenv("BOT_TOKEN", "MAINTOKEN")
        get_settings.cache_clear()
        rec = _Recorder()
        _install_fake_client(monkeypatch, rec)

        await bot_mod.send_html_message(7, "hello")

        assert rec.url is not None
        assert "botMAINTOKEN/sendMessage" in rec.url


# ---------------------------------------------------------------------------
# answer_callback_query
# ---------------------------------------------------------------------------


class TestAnswerCallbackQueryToken:
    async def test_explicit_bot_token_in_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BOT_TOKEN", "MAINTOKEN")
        get_settings.cache_clear()
        rec = _Recorder()
        _install_fake_client(monkeypatch, rec)

        await bot_mod.answer_callback_query(
            "cbq", text="Нет доступа.", show_alert=True, bot_token="IVANX"
        )

        assert rec.url is not None
        assert "botIVANX/answerCallbackQuery" in rec.url
        assert "botMAINTOKEN/" not in rec.url
        assert rec.payload is not None
        assert rec.payload["callback_query_id"] == "cbq"
        assert rec.payload["show_alert"] is True
        assert rec.payload["text"] == "Нет доступа."

    async def test_none_token_uses_main_bot(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BOT_TOKEN", "MAINTOKEN")
        get_settings.cache_clear()
        rec = _Recorder()
        _install_fake_client(monkeypatch, rec)

        await bot_mod.answer_callback_query("cbq")

        assert rec.url is not None
        assert "botMAINTOKEN/answerCallbackQuery" in rec.url
        # Silent ack → no text key.
        assert rec.payload is not None
        assert "text" not in rec.payload

    async def test_text_truncated_to_200_chars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BOT_TOKEN", "MAINTOKEN")
        get_settings.cache_clear()
        rec = _Recorder()
        _install_fake_client(monkeypatch, rec)

        await bot_mod.answer_callback_query("cbq", text="z" * 500, bot_token="T")

        assert rec.payload is not None
        assert len(rec.payload["text"]) == 200
