"""ADR-0027 §11 (round-42) — push-callback authorisation matrix (CRITICAL).

Source of truth: ``backend/app/telegram/callback_handler.py``
(``handle_push_callback_query``).

The authorisation model for push bots is DIFFERENT from the main callback:
rights = membership in ``settings.admin_telegram_ids`` (from ``.env``) PLUS a
defensive group-match (``account.group_id == bot.group_id``). There is no
``telegram_links`` / per-user visibility scope. These tests assert the two
security-critical vectors are closed:

  (a) a non-admin who learned the bot token cannot pull a message body;
  (b) an admin of team X cannot forge ``msg:{id}`` of team Y's message
      through bot X's webhook (group-mismatch → deny).

Every test drives the REAL push-webhook route (with the valid per-bot secret)
so the full router → handler path is exercised, and mocks only the Bot API
border so we can assert exactly which token replied and whether the body was
delivered.
"""

# Cyrillic UI strings in assertions are intentional; silence ruff unicode lints.
# ruff: noqa: RUF001 RUF002 RUF003

from __future__ import annotations

from typing import Any

import httpx
import pytest

from tests.integration.telegram.test_push_webhook_router import (
    _ADMIN_A,
    _ADMIN_B,
    _IVAN_SECRET,
    _IVAN_TOKEN,
    _NON_ADMIN,
    _ApiRecorder,
    _callback_update,
    _post_webhook,
    api_recorder,  # noqa: F401 - re-exported fixture
    configure_push_webhook,  # noqa: F401 - re-exported fixture
    seed_groups,  # noqa: F401 - re-exported fixture
)

pytestmark = pytest.mark.integration


def _alert_texts(rec: _ApiRecorder) -> list[str]:
    """answerCallbackQuery payloads that popped a show_alert toast."""
    return [c["payload"].get("text", "") for c in rec.answer_calls()]


# ===========================================================================
# §11 step 2 — admin membership is the right (CRITICAL)
# ===========================================================================


class TestAdminAuthorisation:
    async def test_admin_in_group_receives_body_via_this_bot_then_ack(
        self,
        configure_push_webhook: None,  # noqa: F811
        api_recorder: _ApiRecorder,  # noqa: F811
        seed_groups: list[int],  # noqa: F811
        client: httpx.AsyncClient,
        super_admin_user: Any,
        create_mail_account: Any,
        create_message: Any,
    ) -> None:
        # account.group_id == bot.group_id (1) AND from.id ∈ admins.
        acc = await create_mail_account(super_admin_user.id, "ok@x.com", group_id=1)
        msg = await create_message(
            acc.id,
            uid=310001,
            subject="Secret Subject",
            from_addr="client@x.com",
            from_name="Client X",
            body_text="confidential body content",
        )

        resp = await _post_webhook(
            client,
            "ivan",
            _callback_update(from_id=_ADMIN_B, data=f"msg:{msg.id}"),
            secret=_IVAN_SECRET,
        )
        assert resp.status_code == 200, resp.text

        # Body delivered via ivan's token.
        body = api_recorder.body_text()
        assert "confidential body content" in body
        assert "Secret Subject" in body
        assert "Client X" in body
        api_recorder.assert_all_use_token(_IVAN_TOKEN)
        # Final silent ack (answerCallbackQuery with no error text).
        answers = api_recorder.answer_calls()
        assert answers, "no answerCallbackQuery ack"
        # The terminal ack carries no error text (body already in chat).
        assert answers[-1]["payload"].get("text") is None

    async def test_non_admin_denied_no_body(
        self,
        configure_push_webhook: None,  # noqa: F811
        api_recorder: _ApiRecorder,  # noqa: F811
        seed_groups: list[int],  # noqa: F811
        client: httpx.AsyncClient,
        super_admin_user: Any,
        create_mail_account: Any,
        create_message: Any,
    ) -> None:
        # from.id NOT in admin_telegram_ids → «Нет доступа», NO body.
        acc = await create_mail_account(super_admin_user.id, "deny@x.com", group_id=1)
        msg = await create_message(acc.id, uid=310002, body_text="MUST NOT LEAK")

        resp = await _post_webhook(
            client,
            "ivan",
            _callback_update(from_id=_NON_ADMIN, data=f"msg:{msg.id}"),
            secret=_IVAN_SECRET,
        )
        assert resp.status_code == 200, resp.text

        # NO sendMessage (the body) at all.
        assert api_recorder.send_message_calls() == [], "body leaked to a non-admin!"
        # An alert «Нет доступа» was shown — via ivan's token.
        alerts = _alert_texts(api_recorder)
        assert any("Нет доступа" in t for t in alerts), alerts
        api_recorder.assert_all_use_token(_IVAN_TOKEN)
        # The deny alert is a show_alert modal.
        deny = api_recorder.answer_calls()[0]
        assert deny["payload"].get("show_alert") is True


# ===========================================================================
# §11 step 5 — defensive group-match (CRITICAL: cross-team forgery)
# ===========================================================================


class TestGroupMatch:
    async def test_foreign_group_message_denied_no_body(
        self,
        configure_push_webhook: None,  # noqa: F811
        api_recorder: _ApiRecorder,  # noqa: F811
        seed_groups: list[int],  # noqa: F811
        client: httpx.AsyncClient,
        super_admin_user: Any,
        create_mail_account: Any,
        create_message: Any,
    ) -> None:
        # The message belongs to GROUP 2 (alexandra's team); the admin forges
        # its msg:{id} through IVAN's webhook (bot.group_id == 1). Even though
        # the tapper IS an admin, the group-mismatch must deny + not leak.
        acc2 = await create_mail_account(super_admin_user.id, "g2@x.com", group_id=2)
        msg = await create_message(acc2.id, uid=310003, body_text="ANOTHER TEAM SECRET")

        resp = await _post_webhook(
            client,
            "ivan",
            _callback_update(from_id=_ADMIN_A, data=f"msg:{msg.id}"),
            secret=_IVAN_SECRET,
        )
        assert resp.status_code == 200, resp.text

        assert api_recorder.send_message_calls() == [], "cross-team body leaked!"
        alerts = _alert_texts(api_recorder)
        assert any("Сообщение недоступно" in t for t in alerts), alerts
        api_recorder.assert_all_use_token(_IVAN_TOKEN)


# ===========================================================================
# §11 step 4 — message / account missing
# ===========================================================================


class TestMissingRows:
    async def test_missing_message_says_unavailable_no_body(
        self,
        configure_push_webhook: None,  # noqa: F811
        api_recorder: _ApiRecorder,  # noqa: F811
        seed_groups: list[int],  # noqa: F811
        client: httpx.AsyncClient,
    ) -> None:
        resp = await _post_webhook(
            client,
            "ivan",
            _callback_update(from_id=_ADMIN_A, data="msg:99999999"),
            secret=_IVAN_SECRET,
        )
        assert resp.status_code == 200, resp.text
        assert api_recorder.send_message_calls() == []
        alerts = _alert_texts(api_recorder)
        assert any("Сообщение больше не доступно" in t for t in alerts), alerts

    async def test_missing_account_says_unavailable_no_body(
        self,
        configure_push_webhook: None,  # noqa: F811
        api_recorder: _ApiRecorder,  # noqa: F811
        seed_groups: list[int],  # noqa: F811
        client: httpx.AsyncClient,
        super_admin_user: Any,
        create_mail_account: Any,
        create_message: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        acc = await create_mail_account(super_admin_user.id, "acc@x.com", group_id=1)
        msg = await create_message(acc.id, uid=310004, body_text="x")

        # Force the account lookup to miss (retention race) — exercises the
        # defensive "account gone" branch.
        from backend.app.repositories.mail_accounts import MailAccountsRepo

        async def _none_get_by_id(self: Any, account_id: int) -> None:
            return None

        monkeypatch.setattr(MailAccountsRepo, "get_by_id", _none_get_by_id)

        resp = await _post_webhook(
            client,
            "ivan",
            _callback_update(from_id=_ADMIN_A, data=f"msg:{msg.id}"),
            secret=_IVAN_SECRET,
        )
        assert resp.status_code == 200, resp.text
        assert api_recorder.send_message_calls() == []
        alerts = _alert_texts(api_recorder)
        assert any("Сообщение больше не доступно" in t for t in alerts), alerts


# ===========================================================================
# §11 step 1 — malformed callback_data
# ===========================================================================


class TestBadCallbackData:
    @pytest.mark.parametrize("data", ["open:5", "msg:", "msg:abc", "delete", "", "msg:5:6"])
    async def test_non_msg_data_unsupported_action_no_body(
        self,
        data: str,
        configure_push_webhook: None,  # noqa: F811
        api_recorder: _ApiRecorder,  # noqa: F811
        seed_groups: list[int],  # noqa: F811
        client: httpx.AsyncClient,
    ) -> None:
        resp = await _post_webhook(
            client, "ivan", _callback_update(from_id=_ADMIN_A, data=data), secret=_IVAN_SECRET
        )
        assert resp.status_code == 200, resp.text
        assert api_recorder.send_message_calls() == []
        alerts = _alert_texts(api_recorder)
        assert any("Неподдерживаемое действие" in t for t in alerts), (data, alerts)
        # The unsupported-action answer is NOT a show_alert (transient toast).
        assert api_recorder.answer_calls()[0]["payload"].get("show_alert") is not True


# ===========================================================================
# §11 step 6 — token isolation + chunk-split + sanitize (test-cases #3, #7)
# ===========================================================================


class TestTokenIsolationAndChunking:
    async def test_reply_uses_push_bot_token_not_main_bot_token(
        self,
        configure_push_webhook: None,  # noqa: F811
        api_recorder: _ApiRecorder,  # noqa: F811
        seed_groups: list[int],  # noqa: F811
        client: httpx.AsyncClient,
        super_admin_user: Any,
        create_mail_account: Any,
        create_message: Any,
    ) -> None:
        from shared.config import get_settings

        main_token = get_settings().BOT_TOKEN
        acc = await create_mail_account(super_admin_user.id, "iso@x.com", group_id=1)
        msg = await create_message(acc.id, uid=310005, body_text="iso body")

        resp = await _post_webhook(
            client,
            "ivan",
            _callback_update(from_id=_ADMIN_A, data=f"msg:{msg.id}"),
            secret=_IVAN_SECRET,
        )
        assert resp.status_code == 200, resp.text
        # EVERY call (sendMessage + answerCallbackQuery) uses ivan's token.
        api_recorder.assert_all_use_token(_IVAN_TOKEN)
        # And NONE of them uses the main bot token.
        if main_token:
            for c in api_recorder.calls:
                assert f"bot{main_token}/" not in c["url"], "leaked main BOT_TOKEN on push reply!"

    async def test_long_body_split_into_at_most_4_chunks(
        self,
        configure_push_webhook: None,  # noqa: F811
        api_recorder: _ApiRecorder,  # noqa: F811
        seed_groups: list[int],  # noqa: F811
        client: httpx.AsyncClient,
        super_admin_user: Any,
        create_mail_account: Any,
        create_message: Any,
    ) -> None:
        # A body far above 4 * 3800 chars → splitter caps at MAX_CHUNKS (4) and
        # marks the last chunk truncated. Each chunk must fit the Telegram limit.
        from backend.app.telegram.callback_handler import (
            _CONTINUATION_MARKER,
            MAX_CHUNKS,
            MAX_TELEGRAM_TEXT_LEN,
        )

        # Lines so rfind("\n") splits cleanly; total >> 4 chunks.
        huge = "\n".join(f"line {i} " + "x" * 100 for i in range(400))
        acc = await create_mail_account(super_admin_user.id, "big@x.com", group_id=1)
        msg = await create_message(acc.id, uid=310006, subject="Big", body_text=huge)

        resp = await _post_webhook(
            client,
            "ivan",
            _callback_update(from_id=_ADMIN_A, data=f"msg:{msg.id}"),
            secret=_IVAN_SECRET,
        )
        assert resp.status_code == 200, resp.text

        sm = api_recorder.send_message_calls()
        assert 1 <= len(sm) <= MAX_CHUNKS, f"expected <= {MAX_CHUNKS} chunks, got {len(sm)}"
        for c in sm:
            assert len(c["payload"]["text"]) <= MAX_TELEGRAM_TEXT_LEN
            assert c["payload"]["parse_mode"] == "HTML"
        # Truncated → last chunk carries the continuation marker.
        assert (
            sm[-1]["payload"]["text"].endswith(_CONTINUATION_MARKER.strip())
            or _CONTINUATION_MARKER in sm[-1]["payload"]["text"]
        )
        api_recorder.assert_all_use_token(_IVAN_TOKEN)

    async def test_html_body_is_sanitized_round39_41(
        self,
        configure_push_webhook: None,  # noqa: F811
        api_recorder: _ApiRecorder,  # noqa: F811
        seed_groups: list[int],  # noqa: F811
        client: httpx.AsyncClient,
        super_admin_user: Any,
        create_mail_account: Any,
        create_message: Any,
    ) -> None:
        # body_html with a disallowed <div>/<table> + blank-line spacers. The
        # round-39/41 sanitize pipeline must strip disallowed tags and collapse
        # blank lines, while keeping an anchor clickable.
        body_html = (
            "<div><table><tr><td>Hello</td></tr></table>"
            "<br><br><br>"
            '<a href="https://example.com/x">link</a></div>'
        )
        acc = await create_mail_account(super_admin_user.id, "html@x.com", group_id=1)
        msg = await create_message(
            acc.id, uid=310007, subject="HtmlMsg", body_text="", body_html=body_html
        )

        resp = await _post_webhook(
            client,
            "ivan",
            _callback_update(from_id=_ADMIN_A, data=f"msg:{msg.id}"),
            secret=_IVAN_SECRET,
        )
        assert resp.status_code == 200, resp.text

        text = api_recorder.body_text()
        # Disallowed structural tags removed.
        assert "<div" not in text
        assert "<table" not in text
        # Anchor preserved (Bot API allows <a>).
        assert 'href="https://example.com/x"' in text
        # round-39: no 3+ consecutive blank lines survive.
        assert "\n\n\n" not in text
