"""ADR-0027 §10/§11 (round-42) — push-only per-team bot webhook + callback.

Source of truth:
- ``backend/app/telegram/router.py`` (``POST /api/telegram/push-webhook/{bot_name}``)
- ``backend/app/telegram/callback_handler.py`` (``handle_push_callback_query``)
- ``backend/app/csrf.py`` (push-webhook prefix exempt)

These tests drive the real ASGI app (FastAPI) end-to-end against the live
Postgres + Redis stack, mocking **only** the Bot API border
(``httpx.AsyncClient`` inside ``backend.app.telegram.bot``) so no real request
leaves the process. The fake records every POST URL + JSON body so we can
assert:

- per-bot token isolation (the reply token is THIS bot's, never ``BOT_TOKEN``);
- the authorisation matrix (admin / non-admin / group-mismatch / missing row);
- the webhook surface (header secret fail-closed, non-callback drop, 404 for
  unknown / unconfigured bot, rate-limit, malformed JSON).

The push feature is enabled by setting ``BOT_*`` env vars + clearing the
lru-cached settings BEFORE the ``app`` fixture is built. ``configure_push_webhook``
is requested first in every test signature so pytest instantiates it before
``client`` (which depends on ``app``). The env is restored + the cache cleared
on teardown so no other test sees the push channel enabled.
"""

# Cyrillic UI strings in assertions are intentional; silence ruff unicode lints.
# ruff: noqa: RUF001 RUF002 RUF003

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from shared.config import get_settings

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Fixed test config: bot names, tokens, secrets, group bindings, admins.
# ivan  -> group 1, secret IVAN_SECRET
# alex  -> group 2, secret "" (configured token+group but NO webhook secret)
# admins: 111, 222
# ---------------------------------------------------------------------------

_IVAN_TOKEN = "IVAN_TOK"
_IVAN_SECRET = "ivansecret_aaaaaaaaaaaaaaaaaaaaaa"
_ALEX_TOKEN = "ALEX_TOK"
_ADMIN_A = 111
_ADMIN_B = 222
_ADMINS_CSV = f"{_ADMIN_A},{_ADMIN_B}"
_NON_ADMIN = 999


@pytest.fixture
def configure_push_webhook(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Configure ivan (group 1, with secret) + alexandra (group 2, no secret).

    The main launcher bot is left as-is (``.env`` provides BOT_TOKEN); we only
    add the push bots. ``BOT_ANDREI`` stays unconfigured (unknown-bot case).
    """
    monkeypatch.setenv("BOT_IVAN_TOKEN", _IVAN_TOKEN)
    monkeypatch.setenv("BOT_IVAN_GROUP_ID", "1")
    monkeypatch.setenv("BOT_IVAN_WEBHOOK_SECRET", _IVAN_SECRET)
    monkeypatch.setenv("BOT_ALEXANDRA_TOKEN", _ALEX_TOKEN)
    monkeypatch.setenv("BOT_ALEXANDRA_GROUP_ID", "2")
    monkeypatch.setenv("BOT_ALEXANDRA_WEBHOOK_SECRET", "")  # configured but no secret
    monkeypatch.setenv("BOT_ANDREI_TOKEN", "")
    monkeypatch.setenv("BOT_ANDREI_GROUP_ID", "0")
    monkeypatch.setenv("BOT_ANDREI_WEBHOOK_SECRET", "")
    monkeypatch.setenv("ADMIN_TELEGRAM_IDS", _ADMINS_CSV)
    get_settings.cache_clear()
    s = get_settings()
    # Sanity: ivan is materialised WITH a secret; alexandra WITHOUT.
    by_name = {b.name: b for b in s.push_team_bots}
    assert by_name["ivan"].webhook_secret == _IVAN_SECRET
    assert by_name["ivan"].group_id == 1
    assert by_name["alexandra"].webhook_secret == ""
    assert s.admin_telegram_ids == [_ADMIN_A, _ADMIN_B]
    yield
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Bot API border recorder: replaces httpx.AsyncClient in bot.py, captures the
# (url, json) of every sendMessage / answerCallbackQuery so we can assert the
# token used and whether the body was delivered.
# ---------------------------------------------------------------------------


class _ApiRecorder:
    """Records the two Bot-API outbound helpers that the push-callback uses.

    We patch ``send_html_message`` / ``answer_callback_query`` at the names the
    callback handler imported them under (``callback_handler.*``). This is the
    real external border (the only place a network call would leave), and —
    unlike monkeypatching ``bot_mod.httpx.AsyncClient`` — it does NOT mutate the
    shared ``httpx`` module that the inbound test ``client`` also uses.

    Each recorded call captures ``method`` (sendMessage / answerCallbackQuery),
    the ``bot_token`` it was invoked with, and the payload, so we can assert
    token isolation and whether the body was delivered. The recorded ``url`` is
    synthesised from the token to keep ``assert_all_use_token`` identical to the
    unit-test style.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rec = self

        async def _fake_send_html_message(
            chat_id: int, text_html: str, *, bot_token: str | None = None
        ) -> None:
            tok = bot_token or get_settings().BOT_TOKEN
            rec.calls.append(
                {
                    "method": "sendMessage",
                    "bot_token": bot_token,
                    "url": f"https://api.telegram.org/bot{tok}/sendMessage",
                    "payload": {
                        "chat_id": chat_id,
                        "text": text_html,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": True,
                    },
                }
            )

        async def _fake_answer_callback_query(
            callback_query_id: str,
            *,
            text: str | None = None,
            show_alert: bool = False,
            bot_token: str | None = None,
        ) -> None:
            tok = bot_token or get_settings().BOT_TOKEN
            payload: dict[str, Any] = {"callback_query_id": callback_query_id}
            if text:
                payload["text"] = text[:200]
            if show_alert:
                payload["show_alert"] = True
            rec.calls.append(
                {
                    "method": "answerCallbackQuery",
                    "bot_token": bot_token,
                    "url": f"https://api.telegram.org/bot{tok}/answerCallbackQuery",
                    "payload": payload,
                }
            )

        # Patch at the handler's import site (it did ``from bot import …``).
        monkeypatch.setattr(
            "backend.app.telegram.callback_handler.send_html_message",
            _fake_send_html_message,
            raising=True,
        )
        monkeypatch.setattr(
            "backend.app.telegram.callback_handler.answer_callback_query",
            _fake_answer_callback_query,
            raising=True,
        )

    # --- assertion helpers -------------------------------------------------

    def methods(self) -> list[str]:
        return [c["method"] for c in self.calls]

    def send_message_calls(self) -> list[dict[str, Any]]:
        return [c for c in self.calls if c["method"] == "sendMessage"]

    def answer_calls(self) -> list[dict[str, Any]]:
        return [c for c in self.calls if c["method"] == "answerCallbackQuery"]

    def assert_all_use_token(self, token: str) -> None:
        for c in self.calls:
            assert c["bot_token"] == token, f"call did not use token {token}: {c['bot_token']}"
            assert f"bot{token}/" in c["url"], f"url did not embed token {token}: {c['url']}"

    def body_text(self) -> str:
        sm = self.send_message_calls()
        assert sm, "no sendMessage (body) was delivered"
        return "\n".join(c["payload"]["text"] for c in sm)


@pytest.fixture
def api_recorder(monkeypatch: pytest.MonkeyPatch) -> _ApiRecorder:
    rec = _ApiRecorder()
    rec.install(monkeypatch)
    return rec


# ---------------------------------------------------------------------------
# DB seeding for groups (push group-match needs real groups + accounts).
# ---------------------------------------------------------------------------


@pytest.fixture
async def seed_groups(db_engine: AsyncEngine) -> list[int]:
    """Create leaderless ``groups`` rows with explicit ids 1, 2."""
    from shared.models import Group

    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with factory() as ses, ses.begin():
        for gid in (1, 2):
            ses.add(Group(id=gid, name=f"team{gid}", leader_user_id=None))
        await ses.flush()
    return [1, 2]


# ---------------------------------------------------------------------------
# Payload builders
# ---------------------------------------------------------------------------


def _callback_update(
    *,
    from_id: int,
    data: str,
    chat_id: int | None = None,
    update_id: int = 1,
    with_message: bool = True,
) -> dict[str, Any]:
    """Build a Telegram ``Update`` envelope carrying a callback_query."""
    cq: dict[str, Any] = {
        "id": "cbq-1",
        "from": {"id": from_id, "first_name": "Adm", "username": "adm"},
        "data": data,
    }
    if with_message:
        cq["message"] = {"chat": {"id": chat_id if chat_id is not None else from_id}}
    return {"update_id": update_id, "callback_query": cq}


def _message_update(
    *, chat_id: int = 111, text: str = "/start", update_id: int = 1
) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "message": {"chat": {"id": chat_id}, "text": text, "from": {"id": chat_id}},
    }


async def _post_webhook(
    client: httpx.AsyncClient,
    bot_name: str,
    body: dict[str, Any] | str,
    *,
    secret: str | None = None,
    raw: bool = False,
) -> httpx.Response:
    headers: dict[str, str] = {}
    if secret is not None:
        headers["X-Telegram-Bot-Api-Secret-Token"] = secret
    url = f"/api/telegram/push-webhook/{bot_name}"
    if raw:
        return await client.post(
            url, content=body, headers={**headers, "content-type": "application/json"}
        )
    return await client.post(url, json=body, headers=headers)  # type: ignore[arg-type]


# ===========================================================================
# 1. push-webhook router — surface / secret / routing (ADR-0027 §10)
# ===========================================================================


class TestPushWebhookSurface:
    async def test_valid_secret_callback_is_processed_200(
        self,
        configure_push_webhook: None,
        api_recorder: _ApiRecorder,
        seed_groups: list[int],
        client: httpx.AsyncClient,
        super_admin_user: Any,
        create_mail_account: Any,
        create_message: Any,
    ) -> None:
        acc = await create_mail_account(super_admin_user.id, "g1@x.com", group_id=1)
        msg = await create_message(acc.id, uid=300001, subject="Hi", body_text="hello body")

        resp = await _post_webhook(
            client,
            "ivan",
            _callback_update(from_id=_ADMIN_A, data=f"msg:{msg.id}"),
            secret=_IVAN_SECRET,
        )
        assert resp.status_code == 200, resp.text
        # The body was delivered + acked — all via ivan's token.
        assert api_recorder.send_message_calls(), "callback body not delivered"
        api_recorder.assert_all_use_token(_IVAN_TOKEN)
        assert "hello body" in api_recorder.body_text()

    async def test_missing_header_is_fail_closed_404(
        self,
        configure_push_webhook: None,
        api_recorder: _ApiRecorder,
        client: httpx.AsyncClient,
    ) -> None:
        # No header at all → fail-closed (unlike the main webhook which tolerates
        # a missing header because the URL-path secret already proved the caller).
        resp = await _post_webhook(
            client, "ivan", _callback_update(from_id=_ADMIN_A, data="msg:1"), secret=None
        )
        assert resp.status_code == 404, resp.text
        assert api_recorder.calls == []

    async def test_wrong_header_secret_404(
        self,
        configure_push_webhook: None,
        api_recorder: _ApiRecorder,
        client: httpx.AsyncClient,
    ) -> None:
        resp = await _post_webhook(
            client,
            "ivan",
            _callback_update(from_id=_ADMIN_A, data="msg:1"),
            secret="WRONG_SECRET_value_000000000000000",
        )
        assert resp.status_code == 404, resp.text
        assert api_recorder.calls == []

    async def test_unknown_bot_name_404(
        self,
        configure_push_webhook: None,
        api_recorder: _ApiRecorder,
        client: httpx.AsyncClient,
    ) -> None:
        # ``andrei`` is unconfigured (no token/group) → unenumerable not_found,
        # even with a syntactically valid-looking header.
        resp = await _post_webhook(
            client,
            "andrei",
            _callback_update(from_id=_ADMIN_A, data="msg:1"),
            secret=_IVAN_SECRET,
        )
        assert resp.status_code == 404, resp.text
        # Also a totally bogus name.
        resp2 = await _post_webhook(
            client,
            "nonexistent",
            _callback_update(from_id=_ADMIN_A, data="msg:1"),
            secret=_IVAN_SECRET,
        )
        assert resp2.status_code == 404, resp2.text
        assert api_recorder.calls == []

    async def test_bot_with_empty_secret_404(
        self,
        configure_push_webhook: None,
        api_recorder: _ApiRecorder,
        client: httpx.AsyncClient,
    ) -> None:
        # ``alexandra`` is configured (token+group) but has an EMPTY webhook
        # secret → it has no push-webhook (the lookup requires a non-empty
        # secret). Any request → 404, even sending alexandra's "secret" (empty).
        resp = await _post_webhook(
            client,
            "alexandra",
            _callback_update(from_id=_ADMIN_A, data="msg:1"),
            secret="",
        )
        assert resp.status_code == 404, resp.text
        # And with ivan's secret (header present but bot has no secret to match).
        resp2 = await _post_webhook(
            client,
            "alexandra",
            _callback_update(from_id=_ADMIN_A, data="msg:1"),
            secret=_IVAN_SECRET,
        )
        assert resp2.status_code == 404, resp2.text
        assert api_recorder.calls == []

    @pytest.mark.parametrize(
        "update",
        [
            {"update_id": 5, "message": {"chat": {"id": 111}, "text": "/start"}},
            {"update_id": 6, "message": {"chat": {"id": 111}, "text": "hello"}},
            {"update_id": 7, "edited_message": {"chat": {"id": 111}, "text": "x"}},
        ],
    )
    async def test_non_callback_update_dropped_200_no_dispatch(
        self,
        update: dict[str, Any],
        configure_push_webhook: None,
        api_recorder: _ApiRecorder,
        client: httpx.AsyncClient,
    ) -> None:
        # Push bots accept ONLY callback_query. /start / message / edited_message
        # are silently dropped (200) and NEVER reach handle_update / dispatch.
        resp = await _post_webhook(client, "ivan", update, secret=_IVAN_SECRET)
        assert resp.status_code == 200, resp.text
        # No Bot-API call at all (no reply, no launcher webapp button).
        assert api_recorder.calls == []

    async def test_malformed_json_200(
        self,
        configure_push_webhook: None,
        api_recorder: _ApiRecorder,
        client: httpx.AsyncClient,
    ) -> None:
        resp = await _post_webhook(client, "ivan", "{not json", secret=_IVAN_SECRET, raw=True)
        assert resp.status_code == 200, resp.text
        assert api_recorder.calls == []

    async def test_csrf_exempt_no_cookie_not_rejected(
        self,
        configure_push_webhook: None,
        api_recorder: _ApiRecorder,
        client: httpx.AsyncClient,
    ) -> None:
        # The request carries NO session cookie / CSRF token. If the prefix were
        # not CSRF-exempt this POST would be a 403 csrf_failed. It must instead
        # pass CSRF and be handled (404 here because secret is wrong — proving we
        # got PAST the CSRF middleware to the route's own secret check).
        resp = await _post_webhook(
            client, "ivan", _callback_update(from_id=_ADMIN_A, data="msg:1"), secret="bad"
        )
        # Not a 403 CSRF error → CSRF exemption is in effect.
        assert resp.status_code == 404, resp.text
        body = resp.json()
        assert body.get("error", {}).get("code") != "csrf_failed"


# ===========================================================================
# Rate-limit (shared _LIMIT_TG_WEBHOOK, 60/min per IP)
# ===========================================================================


class TestPushWebhookRateLimit:
    async def test_over_limit_returns_429(
        self,
        configure_push_webhook: None,
        api_recorder: _ApiRecorder,
        client: httpx.AsyncClient,
    ) -> None:
        # 60/min per IP. The 61st request in the window → 429. We send to an
        # unknown bot (cheap 404) so the only thing under test is the limiter,
        # which runs FIRST (before any secret/bot work) per ADR-0027 §10.
        last: httpx.Response | None = None
        for _ in range(61):
            last = await _post_webhook(
                client, "ivan", _callback_update(from_id=_ADMIN_A, data="msg:1"), secret="x"
            )
        assert last is not None
        assert last.status_code == 429, last.text
