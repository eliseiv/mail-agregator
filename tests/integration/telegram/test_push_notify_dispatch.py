"""ADR-0027 §3/§5/§9 — push-only per-team bot dispatcher behaviour.

Source of truth: ``worker/app/push_notify_dispatch.py``.

These tests drive the dispatcher end-to-end against the real Postgres + Redis
while mocking only the Bot API border (``send_notification``). They cover:

- Happy path: a message of group 1 (bot ``ivan`` configured) → exactly one
  ``send_notification`` per admin id, with that bot's token and
  ``with_button=False``. No duplicates.
- Skips: message of a group without a bot; ``account.group_id`` None; missing
  message / missing account — all skip without raising.
- ISOLATION (critical): the ``ivan`` bot only receives its own team's
  messages, never another team's.
- Fire-and-forget: dead / retry_after / transient outcomes write NOTHING to
  the DB and never re-enqueue — only logs.
- Text format identical to round-36 (🆔 / #️⃣ / Клиент / Тема / preview) with
  NO team label.
- Malformed payload / a single item error → the tick continues (never raises).

We configure the push bots by setting env vars + clearing the lru-cached
settings; the autouse ``_restore_push_env`` fixture resets them afterwards so
no other test sees the push feature enabled.
"""

# Cyrillic notification labels are intentional; silence ruff's unicode lints.
# ruff: noqa: RUF001 RUF002 RUF003

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from backend.app.telegram import bot as bot_mod
from shared.config import get_settings
from shared.models import TelegramNotification
from shared.redis_client import get_redis
from worker.app import push_notify_dispatch as pnd

pytestmark = pytest.mark.integration

_QUEUE = pnd._QUEUE_KEY
_ADMINS = "111,222"


# ---------------------------------------------------------------------------
# Fixtures: configure the push feature + record send_notification calls
# ---------------------------------------------------------------------------


@pytest.fixture
async def seed_groups(db_engine: AsyncEngine) -> list[int]:
    """Create leaderless ``groups`` rows with explicit ids 1, 2, 3.

    ``mail_accounts.group_id`` has a FK to ``groups.id``; the push bots are
    bound to group ids 1 (ivan) / 2 (alexandra) / 3 (no bot). We force the ids
    so the ADR-0027 prod mapping holds regardless of the truncate-restarted
    sequence (``groups`` is wiped via the users CASCADE between tests).
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from shared.models import Group

    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with factory() as ses, ses.begin():
        for gid in (1, 2, 3):
            ses.add(Group(id=gid, name=f"team{gid}", leader_user_id=None))
        await ses.flush()
    return [1, 2, 3]


@pytest.fixture
def configure_push(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Configure ivan=group1, alexandra=group2 push bots + two admin ids.

    Group 3 deliberately has NO bot (covers the "group without a bot" skip).
    """
    monkeypatch.setenv("BOT_IVAN_TOKEN", "IVAN_TOK")
    monkeypatch.setenv("BOT_IVAN_GROUP_ID", "1")
    monkeypatch.setenv("BOT_ALEXANDRA_TOKEN", "ALEX_TOK")
    monkeypatch.setenv("BOT_ALEXANDRA_GROUP_ID", "2")
    monkeypatch.setenv("BOT_ANDREI_TOKEN", "")
    monkeypatch.setenv("BOT_ANDREI_GROUP_ID", "0")
    monkeypatch.setenv("ADMIN_TELEGRAM_IDS", _ADMINS)
    get_settings.cache_clear()
    s = get_settings()
    assert s.push_team_bots_enabled is True
    assert {b.name for b in s.push_team_bots} == {"ivan", "alexandra"}
    yield
    get_settings.cache_clear()


class _SendRecorder:
    """Records every ``send_notification`` call + returns a scripted outcome."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self._kind = "ok"
        self._extra: dict[str, Any] = {"telegram_message_id": 1}

    def set_outcome(self, kind: str, **extra: Any) -> None:
        self._kind = kind
        self._extra = extra

    async def __call__(
        self,
        *,
        chat_id: int,
        text_html: str,
        message_id: int,
        bot_token: str | None = None,
        with_button: bool = True,
    ) -> Any:
        self.calls.append(
            {
                "chat_id": chat_id,
                "text_html": text_html,
                "message_id": message_id,
                "bot_token": bot_token,
                "with_button": with_button,
            }
        )
        return bot_mod.SendNotificationResult(kind=self._kind, **self._extra)  # type: ignore[arg-type]


@pytest.fixture
def fake_send(monkeypatch: pytest.MonkeyPatch) -> _SendRecorder:
    """Patch the Bot API border that ``_dispatch_one`` imports lazily."""
    rec = _SendRecorder()
    monkeypatch.setattr("backend.app.telegram.bot.send_notification", rec, raising=True)
    return rec


# ---------------------------------------------------------------------------
# Local helpers
# ---------------------------------------------------------------------------


def _payload(message_id: int, source: str = "sync") -> str:
    return json.dumps({"v": 1, "message_id": int(message_id), "source": source})


async def _count_notifications(db_engine: AsyncEngine, message_id: int) -> int:
    from sqlalchemy import func, select
    from sqlalchemy.ext.asyncio import async_sessionmaker

    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with factory() as ses:
        return (
            await ses.execute(
                select(func.count())
                .select_from(TelegramNotification)
                .where(TelegramNotification.message_id == message_id)
            )
        ).scalar_one()


# ---------------------------------------------------------------------------
# Happy path + per-admin fan-out
# ---------------------------------------------------------------------------


class TestHappyPath:
    async def test_group_with_bot_sends_to_each_admin_no_dup(
        self,
        db_engine: AsyncEngine,
        client: Any,
        configure_push: None,
        seed_groups: list[int],
        fake_send: _SendRecorder,
        super_admin_user: Any,
        create_mail_account: Any,
        create_message: Any,
    ) -> None:
        # group_id=1 is ivan's group (seeded by ``seed_groups``).
        acc = await create_mail_account(
            super_admin_user.id, "team1@example.com", display_name="Team One", group_id=1
        )
        msg = await create_message(
            acc.id, uid=210001, subject="Hi", from_addr="c@x.com", from_name="Client X"
        )

        r = get_redis()
        await r.lpush(_QUEUE, _payload(msg.id))

        await pnd.push_notify_dispatch()

        # Exactly one send per admin (2 admins) — no duplicates.
        assert len(fake_send.calls) == 2
        by_chat = {c["chat_id"]: c for c in fake_send.calls}
        assert set(by_chat) == {111, 222}
        for c in fake_send.calls:
            assert c["bot_token"] == "IVAN_TOK"
            assert c["with_button"] is False
            assert c["message_id"] == msg.id

        # Fire-and-forget: no telegram_notifications rows written.
        assert await _count_notifications(db_engine, msg.id) == 0
        # Queue fully drained, nothing re-enqueued.
        assert await r.llen(_QUEUE) == 0

    async def test_text_is_round36_format_without_team_label(
        self,
        db_engine: AsyncEngine,
        client: Any,
        configure_push: None,
        seed_groups: list[int],
        fake_send: _SendRecorder,
        super_admin_user: Any,
        create_mail_account: Any,
        create_message: Any,
        tag_message_for_user: Any,
    ) -> None:
        acc = await create_mail_account(
            super_admin_user.id, "fmt@example.com", display_name="Fmt Box", group_id=1
        )
        msg = await create_message(
            acc.id,
            uid=210101,
            subject="Quarterly",
            from_addr="sender@x.com",
            from_name="Sender Z",
            body_text="The body preview text here.",
        )
        await tag_message_for_user(super_admin_user.id, msg.id, "VIP")

        r = get_redis()
        await r.lpush(_QUEUE, _payload(msg.id))
        await pnd.push_notify_dispatch()

        assert len(fake_send.calls) == 2
        text = fake_send.calls[0]["text_html"]
        # Round-36 card markers.
        assert "🆔" in text
        assert "#️⃣" in text
        assert "Клиент" in text
        assert "Тема: <b>Quarterly</b>" in text
        assert "The body preview text here." in text
        assert "VIP" in text
        # No team/bot label leaks into the text (the bot itself = the team).
        assert "ivan" not in text.lower()


# ---------------------------------------------------------------------------
# Skips (ADR-0027 §9)
# ---------------------------------------------------------------------------


class TestSkips:
    async def test_group_without_bot_is_skipped(
        self,
        db_engine: AsyncEngine,
        client: Any,
        configure_push: None,
        seed_groups: list[int],
        fake_send: _SendRecorder,
        super_admin_user: Any,
        create_mail_account: Any,
        create_message: Any,
    ) -> None:
        # group_id=3 has NO configured bot (andrei token empty).
        acc = await create_mail_account(super_admin_user.id, "team3@example.com", group_id=3)
        msg = await create_message(acc.id, uid=210201)

        r = get_redis()
        await r.lpush(_QUEUE, _payload(msg.id))
        await pnd.push_notify_dispatch()

        assert fake_send.calls == []
        assert await r.llen(_QUEUE) == 0

    async def test_account_without_group_is_skipped(
        self,
        db_engine: AsyncEngine,
        client: Any,
        configure_push: None,
        fake_send: _SendRecorder,
        super_admin_user: Any,
        create_mail_account: Any,
        create_message: Any,
    ) -> None:
        # super_admin has no group → account.group_id is None.
        acc = await create_mail_account(super_admin_user.id, "nogroup@example.com", group_id=None)
        msg = await create_message(acc.id, uid=210301)

        r = get_redis()
        await r.lpush(_QUEUE, _payload(msg.id))
        await pnd.push_notify_dispatch()

        assert fake_send.calls == []

    async def test_missing_message_is_skipped(
        self,
        db_engine: AsyncEngine,
        client: Any,
        configure_push: None,
        fake_send: _SendRecorder,
    ) -> None:
        r = get_redis()
        await r.lpush(_QUEUE, _payload(99_999_999))  # no such message
        await pnd.push_notify_dispatch()  # must not raise
        assert fake_send.calls == []

    async def test_missing_account_is_skipped(
        self,
        db_engine: AsyncEngine,
        client: Any,
        configure_push: None,
        seed_groups: list[int],
        fake_send: _SendRecorder,
        super_admin_user: Any,
        create_mail_account: Any,
        create_message: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The message exists but its account is gone by dispatch time (retention
        # race, ADR-0027 §9). A FK keeps ``messages.mail_account_id`` valid, so
        # we force the repo lookup to miss to exercise the defensive branch.
        acc = await create_mail_account(super_admin_user.id, "gone@example.com", group_id=1)
        msg = await create_message(acc.id, uid=210401)

        from backend.app.repositories.mail_accounts import MailAccountsRepo

        async def _none_get_by_id(self: Any, account_id: int) -> None:
            return None

        monkeypatch.setattr(MailAccountsRepo, "get_by_id", _none_get_by_id)

        r = get_redis()
        await r.lpush(_QUEUE, _payload(msg.id))
        await pnd.push_notify_dispatch()  # must not raise
        assert fake_send.calls == []


# ---------------------------------------------------------------------------
# Isolation (critical, ADR-0027 §3/§7)
# ---------------------------------------------------------------------------


class TestIsolation:
    async def test_each_bot_only_gets_its_own_teams_mail(
        self,
        db_engine: AsyncEngine,
        client: Any,
        configure_push: None,
        seed_groups: list[int],
        fake_send: _SendRecorder,
        super_admin_user: Any,
        create_mail_account: Any,
        create_message: Any,
    ) -> None:
        acc1 = await create_mail_account(super_admin_user.id, "g1@example.com", group_id=1)
        acc2 = await create_mail_account(super_admin_user.id, "g2@example.com", group_id=2)
        m1 = await create_message(acc1.id, uid=210501, subject="for ivan")
        m2 = await create_message(acc2.id, uid=210502, subject="for alexandra")

        r = get_redis()
        await r.lpush(_QUEUE, _payload(m1.id), _payload(m2.id))
        await pnd.push_notify_dispatch()

        # 2 messages × 2 admins = 4 calls.
        assert len(fake_send.calls) == 4
        # Group the tokens used per message.
        tok_by_msg: dict[int, set[str]] = {}
        for c in fake_send.calls:
            tok_by_msg.setdefault(c["message_id"], set()).add(c["bot_token"])
        # ivan's bot only got m1; alexandra's only got m2.
        assert tok_by_msg[m1.id] == {"IVAN_TOK"}
        assert tok_by_msg[m2.id] == {"ALEX_TOK"}


# ---------------------------------------------------------------------------
# Fire-and-forget outcomes (ADR-0027 §5)
# ---------------------------------------------------------------------------


class TestFireAndForget:
    @pytest.mark.parametrize(
        ("kind", "extra"),
        [
            ("dead", {"detail": "Forbidden: bot was blocked"}),
            ("retry_after", {"retry_after_sec": 5}),
            ("transient", {"detail": "http_500"}),
        ],
    )
    async def test_non_ok_outcome_writes_nothing_and_does_not_requeue(
        self,
        kind: str,
        extra: dict[str, Any],
        db_engine: AsyncEngine,
        client: Any,
        configure_push: None,
        seed_groups: list[int],
        fake_send: _SendRecorder,
        super_admin_user: Any,
        create_mail_account: Any,
        create_message: Any,
    ) -> None:
        fake_send.set_outcome(kind, **extra)
        acc = await create_mail_account(super_admin_user.id, f"faf_{kind}@example.com", group_id=1)
        msg = await create_message(acc.id, uid=210600 + len(kind))

        r = get_redis()
        await r.lpush(_QUEUE, _payload(msg.id))
        await pnd.push_notify_dispatch()

        # Send was still attempted for every admin.
        assert len(fake_send.calls) == 2
        # But nothing persisted and nothing re-enqueued (fire-and-forget).
        assert await _count_notifications(db_engine, msg.id) == 0
        assert await r.llen(_QUEUE) == 0


# ---------------------------------------------------------------------------
# Resilience: malformed payload / disabled feature
# ---------------------------------------------------------------------------


class TestResilience:
    async def test_malformed_payload_does_not_crash_tick(
        self,
        db_engine: AsyncEngine,
        client: Any,
        configure_push: None,
        seed_groups: list[int],
        fake_send: _SendRecorder,
        super_admin_user: Any,
        create_mail_account: Any,
        create_message: Any,
    ) -> None:
        acc = await create_mail_account(super_admin_user.id, "mixed@example.com", group_id=1)
        good = await create_message(acc.id, uid=210701)

        r = get_redis()
        # One bad item, one missing-field item, one good item — the tick must
        # process the good one and not raise on the bad ones.
        await r.lpush(_QUEUE, "not-json-at-all")
        await r.lpush(_QUEUE, json.dumps({"no_message_id": True}))
        await r.lpush(_QUEUE, _payload(good.id))

        await pnd.push_notify_dispatch()  # must not raise

        # The good message still produced its 2 admin sends.
        good_calls = [c for c in fake_send.calls if c["message_id"] == good.id]
        assert len(good_calls) == 2
        assert await r.llen(_QUEUE) == 0

    async def test_disabled_feature_drains_nothing(
        self,
        db_engine: AsyncEngine,
        client: Any,
        fake_send: _SendRecorder,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # No push env set → feature disabled → dispatcher returns immediately,
        # WITHOUT popping the queue (so a stray item survives).
        monkeypatch.delenv("BOT_IVAN_TOKEN", raising=False)
        monkeypatch.delenv("ADMIN_TELEGRAM_IDS", raising=False)
        get_settings.cache_clear()
        assert get_settings().push_team_bots_enabled is False

        r = get_redis()
        await r.lpush(_QUEUE, _payload(123))
        await pnd.push_notify_dispatch()

        assert fake_send.calls == []
        # Item NOT drained (feature off never LPOPs).
        assert await r.llen(_QUEUE) == 1
        # Clean up the stray item so the autouse redis-flush isn't relied on.
        await r.delete(_QUEUE)
        get_settings.cache_clear()
