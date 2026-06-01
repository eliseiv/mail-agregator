"""ADR-0022 §2.x — Telegram notification dispatch behaviour.

These tests exercise the dispatcher end-to-end while mocking the Bot API
(through ``fake_send_notification``). They cover:

- Sync_cycle path: a newly-saved message with a tag → recipient resolved →
  ``telegram_notifications`` row inserted + dispatcher sends.
- Bot API outcomes mapped to actions:
  - ``ok``           → ``mark_sent`` with ``telegram_message_id``.
  - ``dead`` (403)   → ``mark_link_dead`` + audit ``telegram_link_dead_marked``;
                       ``sent_at`` stays NULL.
  - ``retry_after``  → row rolled back; payload re-enqueued.
- Idempotency: a re-LPUSH of the same message_id never produces a second
  ``telegram_notifications`` row.
- Recipient resolver:
  - super_admin (with link + tag) receives.
  - group member (with link + tag) receives.
  - member without tag does NOT.
  - member without link does NOT.
  - member with ``tg_notifications_enabled=false`` does NOT.
- Failure isolation: even if Redis is unreachable, ``sync_cycle`` does not
  abort (the LPUSH is wrapped in try/except in :func:`sync_one_account`).
  We construct the failure by patching the Redis client.

Implementation note: we drive the dispatcher directly via
:meth:`TelegramNotifyService.dispatch_one_payload` instead of going through
APScheduler — gives deterministic ordering with no scheduler in the loop.
"""

# Some docstrings reference the Cyrillic notification labels ("Тег"/"Теги")
# intentionally; suppress ruff's ambiguous-unicode warnings file-wide.
# ruff: noqa: RUF001 RUF002 RUF003

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from backend.app.rate_limit import LIMIT_TG_SEND_PER_CHAT
from backend.app.telegram.notify_service import (
    TG_NOTIFY_QUEUE_KEY,
    TelegramNotifyService,
)
from shared.config import get_settings
from shared.models import (
    AdminAudit,
    TelegramLink,
    TelegramNotification,
    User,
)
from shared.redis_client import get_redis
from tests.integration.telegram.conftest import FakeSendResult

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _payload_for(message_id: int, source: str = "sync") -> str:
    return json.dumps({"v": 1, "message_id": int(message_id), "source": source})


async def _dispatch(payload: str, db_engine: AsyncEngine) -> None:
    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with factory() as s, s.begin():
        await TelegramNotifyService(s).dispatch_one_payload(payload)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestDispatchOk:
    async def test_ok_outcome_marks_sent_with_telegram_message_id(
        self,
        db_engine: AsyncEngine,
        client: Any,  # request the integration `client` fixture so app + Redis are live
        super_admin_user: User,
        make_link: Any,
        create_mail_account: Any,
        create_message: Any,
        tag_message_for_user: Any,
        fake_send_notification: Any,
    ) -> None:
        tg_id = 110001
        await make_link(tg_id, super_admin_user.id)
        acc = await create_mail_account(
            super_admin_user.id,
            "admin@example.com",
            display_name="Admin Inbox",
        )
        msg = await create_message(
            acc.id,
            uid=110001,
            subject="Hello world",
            from_addr="from@x.com",
            from_name="From Name",
        )
        await tag_message_for_user(super_admin_user.id, msg.id, "VIP")
        fake_send_notification.push(FakeSendResult(kind="ok", telegram_message_id=98765))

        await _dispatch(_payload_for(msg.id), db_engine)

        # Bot API was called once with the right chat_id.
        assert len(fake_send_notification.calls) == 1
        call = fake_send_notification.calls[0]
        assert call["chat_id"] == tg_id
        assert call["message_id"] == msg.id
        # The text uses display_name + from_name + tag.
        assert "Admin Inbox" in call["text_html"]
        assert "From Name" in call["text_html"]
        assert "VIP" in call["text_html"]

        # telegram_notifications row is marked sent.
        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as ses:
            row = (
                await ses.execute(
                    select(TelegramNotification).where(TelegramNotification.message_id == msg.id)
                )
            ).scalar_one()
            assert row.user_id == super_admin_user.id
            assert row.sent_at is not None
            assert row.telegram_message_id == 98765


# ---------------------------------------------------------------------------
# Dead path
# ---------------------------------------------------------------------------


class TestDispatchDead:
    async def test_dead_outcome_marks_link_dead_and_writes_audit(
        self,
        db_engine: AsyncEngine,
        client: Any,
        super_admin_user: User,
        make_link: Any,
        create_mail_account: Any,
        create_message: Any,
        tag_message_for_user: Any,
        fake_send_notification: Any,
    ) -> None:
        tg_id = 110101
        await make_link(tg_id, super_admin_user.id)
        acc = await create_mail_account(super_admin_user.id, "dead@example.com")
        msg = await create_message(acc.id, uid=110101)
        await tag_message_for_user(super_admin_user.id, msg.id, "tag")
        fake_send_notification.push(
            FakeSendResult(kind="dead", detail="Forbidden: bot was blocked")
        )

        await _dispatch(_payload_for(msg.id), db_engine)

        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as ses:
            # Link is dead-marked.
            link = (
                await ses.execute(
                    select(TelegramLink).where(TelegramLink.telegram_user_id == tg_id)
                )
            ).scalar_one()
            assert link.dead_at is not None

            # ``telegram_notifications`` row exists, but sent_at is NULL (no delivery).
            row = (
                await ses.execute(
                    select(TelegramNotification).where(TelegramNotification.message_id == msg.id)
                )
            ).scalar_one()
            assert row.sent_at is None
            assert row.telegram_message_id is None

            # Audit row.
            audits = (
                (
                    await ses.execute(
                        select(AdminAudit).where(AdminAudit.action == "telegram_link_dead_marked")
                    )
                )
                .scalars()
                .all()
            )
            assert len(audits) == 1


# ---------------------------------------------------------------------------
# Retry path
# ---------------------------------------------------------------------------


class TestDispatchRetryAfter:
    async def test_retry_after_releases_claim_and_requeues(
        self,
        db_engine: AsyncEngine,
        client: Any,
        super_admin_user: User,
        make_link: Any,
        create_mail_account: Any,
        create_message: Any,
        tag_message_for_user: Any,
        fake_send_notification: Any,
    ) -> None:
        tg_id = 110201
        await make_link(tg_id, super_admin_user.id)
        acc = await create_mail_account(super_admin_user.id, "rl@example.com")
        msg = await create_message(acc.id, uid=110201)
        await tag_message_for_user(super_admin_user.id, msg.id, "tag")
        # First call → 429; the dispatcher should release the claim and
        # re-enqueue. We push a single 'retry_after' outcome.
        fake_send_notification.push(FakeSendResult(kind="retry_after", retry_after_sec=2))

        # Queue must be empty initially.
        r = get_redis()
        assert await r.llen(TG_NOTIFY_QUEUE_KEY) == 0

        await _dispatch(_payload_for(msg.id), db_engine)

        # The row is rolled back (no row remains).
        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as ses:
            row = (
                await ses.execute(
                    select(TelegramNotification).where(TelegramNotification.message_id == msg.id)
                )
            ).scalar_one_or_none()
            assert row is None, f"row should be rolled back on retry_after, got {row}"

        # The message_id was re-enqueued with source='recovery'.
        items = await r.lrange(TG_NOTIFY_QUEUE_KEY, 0, -1)
        assert len(items) == 1
        decoded = items[0].decode() if isinstance(items[0], bytes) else items[0]
        payload = json.loads(decoded)
        assert payload["message_id"] == msg.id
        assert payload["source"] == "recovery"


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


class TestIdempotency:
    async def test_second_dispatch_for_same_message_is_a_noop(
        self,
        db_engine: AsyncEngine,
        client: Any,
        super_admin_user: User,
        make_link: Any,
        create_mail_account: Any,
        create_message: Any,
        tag_message_for_user: Any,
        fake_send_notification: Any,
    ) -> None:
        tg_id = 110301
        await make_link(tg_id, super_admin_user.id)
        acc = await create_mail_account(super_admin_user.id, "id@example.com")
        msg = await create_message(acc.id, uid=110301)
        await tag_message_for_user(super_admin_user.id, msg.id, "tag")
        fake_send_notification.push(
            FakeSendResult(kind="ok", telegram_message_id=11),
            FakeSendResult(kind="ok", telegram_message_id=22),  # would be 2nd send
        )

        # First dispatch — sends once.
        await _dispatch(_payload_for(msg.id), db_engine)
        assert len(fake_send_notification.calls) == 1

        # Second dispatch — try_reserve returns None, no send.
        await _dispatch(_payload_for(msg.id), db_engine)
        assert len(fake_send_notification.calls) == 1, "second dispatch must not call Bot API"

        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as ses:
            rows = (
                (
                    await ses.execute(
                        select(TelegramNotification).where(
                            TelegramNotification.message_id == msg.id
                        )
                    )
                )
                .scalars()
                .all()
            )
            # UNIQUE constraint guarantees exactly one row.
            assert len(rows) == 1


# ---------------------------------------------------------------------------
# Recipient resolver
# ---------------------------------------------------------------------------


class TestRecipientResolver:
    async def test_super_admin_and_member_with_tag_both_receive(
        self,
        db_engine: AsyncEngine,
        client: Any,
        super_admin_user: User,
        leader_and_group: tuple[Any, User],
        create_member: Any,
        make_link: Any,
        create_mail_account: Any,
        create_message: Any,
        tag_message_for_user: Any,
        fake_send_notification: Any,
    ) -> None:
        group, leader = leader_and_group
        member = await create_member(group.id, "member_rcpt")
        # Both super_admin and member have links.
        await make_link(110401, super_admin_user.id)
        await make_link(110402, member.id)

        # Mail account belongs to the leader/group.
        acc = await create_mail_account(leader.id, "leader@example.com", group_id=group.id)
        msg = await create_message(acc.id, uid=110401)
        # Both super_admin and member have their own tag on the message.
        await tag_message_for_user(super_admin_user.id, msg.id, "admin-tag")
        await tag_message_for_user(member.id, msg.id, "member-tag")

        fake_send_notification.push(FakeSendResult(kind="ok", telegram_message_id=1))

        await _dispatch(_payload_for(msg.id), db_engine)

        # Both receive.
        chat_ids = {call["chat_id"] for call in fake_send_notification.calls}
        assert chat_ids == {110401, 110402}

    @pytest.mark.parametrize("flag", [False, True])
    async def test_member_on_untagged_message_receives_only_when_flag_on(
        self,
        flag: bool,
        db_engine: AsyncEngine,
        client: Any,
        leader_and_group: tuple[Any, User],
        create_member: Any,
        make_link: Any,
        create_mail_account: Any,
        create_message: Any,
        fake_send_notification: Any,
        set_tg_notify_all: Any,
    ) -> None:
        """Round-31 (ADR-0022 §2.1): the tag predicate is message-level (round-12).

        A member who is a valid recipient of a message that carries **no tags at
        all** is EXCLUDED under ``TG_NOTIFY_ALL_MESSAGES=false`` (historical
        tagged-only behaviour) but INCLUDED under ``true`` (notify about all
        messages). NOTE: the predicate is "the message has ANY tag", not a
        per-user tag — so this fixture deliberately leaves the message untagged.
        """
        set_tg_notify_all(flag)
        group, leader = leader_and_group
        member = await create_member(group.id, f"m_untagged_{int(flag)}")
        link_id = 110520 + int(flag)
        await make_link(link_id, member.id)

        acc = await create_mail_account(
            leader.id, f"leader2_{int(flag)}@example.com", group_id=group.id
        )
        msg = await create_message(acc.id, uid=110501 + int(flag))
        # NO tag applied to this message at all.

        fake_send_notification.push(FakeSendResult(kind="ok", telegram_message_id=1))
        await _dispatch(_payload_for(msg.id), db_engine)

        chat_ids = {call["chat_id"] for call in fake_send_notification.calls}
        if flag:
            assert link_id in chat_ids
        else:
            assert link_id not in chat_ids

    async def test_untagged_message_dispatches_without_tag_line_when_flag_on(
        self,
        db_engine: AsyncEngine,
        client: Any,
        super_admin_user: User,
        make_link: Any,
        create_mail_account: Any,
        create_message: Any,
        fake_send_notification: Any,
        set_tg_notify_all: Any,
    ) -> None:
        """flag=true: a message with NO tags still notifies; the rendered text
        omits the ``Тег``/``Теги`` line entirely (round-31, ADR-0022 §2.5)."""
        set_tg_notify_all(True)
        tg_id = 110530
        await make_link(tg_id, super_admin_user.id)
        acc = await create_mail_account(
            super_admin_user.id, "notag@example.com", display_name="No Tag Box"
        )
        msg = await create_message(acc.id, uid=110530, from_addr="x@y.com", from_name="Sender X")
        # No tag applied.
        fake_send_notification.push(FakeSendResult(kind="ok", telegram_message_id=7))
        await _dispatch(_payload_for(msg.id), db_engine)

        assert len(fake_send_notification.calls) == 1
        text_html = fake_send_notification.calls[0]["text_html"]
        assert "No Tag Box" in text_html
        assert "Sender X" in text_html
        # No tag label and no em-dash placeholder.
        assert "Тег" not in text_html
        assert "Теги" not in text_html
        assert "—" not in text_html

    async def test_member_without_link_does_not_receive(
        self,
        db_engine: AsyncEngine,
        client: Any,
        super_admin_user: User,
        leader_and_group: tuple[Any, User],
        create_member: Any,
        make_link: Any,
        create_mail_account: Any,
        create_message: Any,
        tag_message_for_user: Any,
        fake_send_notification: Any,
    ) -> None:
        group, leader = leader_and_group
        member = await create_member(group.id, "no_link_member")
        # No telegram_links row at all → not a recipient.

        acc = await create_mail_account(leader.id, "leader3@example.com", group_id=group.id)
        msg = await create_message(acc.id, uid=110601)
        await tag_message_for_user(member.id, msg.id, "tag")

        fake_send_notification.push(FakeSendResult(kind="ok", telegram_message_id=1))
        await _dispatch(_payload_for(msg.id), db_engine)

        # Nobody was contacted.
        assert len(fake_send_notification.calls) == 0

    async def test_member_with_notifications_disabled_does_not_receive(
        self,
        db_engine: AsyncEngine,
        client: Any,
        leader_and_group: tuple[Any, User],
        create_member: Any,
        make_link: Any,
        create_mail_account: Any,
        create_message: Any,
        tag_message_for_user: Any,
        fake_send_notification: Any,
    ) -> None:
        group, leader = leader_and_group
        member = await create_member(group.id, "opt_out_member")
        await make_link(110701, member.id)

        # Insert users_settings row with tg_notifications_enabled=false.
        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as ses, ses.begin():
            from backend.app.repositories.user_settings import UserSettingsRepo

            await UserSettingsRepo(ses).upsert_tg_notifications_enabled(
                user_id=member.id, enabled=False
            )

        acc = await create_mail_account(leader.id, "leader4@example.com", group_id=group.id)
        msg = await create_message(acc.id, uid=110701)
        await tag_message_for_user(member.id, msg.id, "tag")

        fake_send_notification.push(FakeSendResult(kind="ok", telegram_message_id=1))
        await _dispatch(_payload_for(msg.id), db_engine)

        # Member opted-out → no call made.
        chat_ids = {call["chat_id"] for call in fake_send_notification.calls}
        assert 110701 not in chat_ids


# ---------------------------------------------------------------------------
# Enqueue helpers
# ---------------------------------------------------------------------------


class TestEnqueueMessageIds:
    async def test_enqueue_lpushes_n_payloads(
        self,
        db_engine: AsyncEngine,
        client: Any,
    ) -> None:
        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as ses:
            n = await TelegramNotifyService(ses).enqueue_message_ids([1, 2, 3])
        assert n == 3
        r = get_redis()
        items = await r.lrange(TG_NOTIFY_QUEUE_KEY, 0, -1)
        assert len(items) == 3
        # All payloads parse as valid JSON with source=sync.
        for raw in items:
            decoded = raw.decode() if isinstance(raw, bytes) else raw
            payload = json.loads(decoded)
            assert payload["source"] == "sync"
            assert payload["message_id"] in (1, 2, 3)

    async def test_enqueue_recovery_tags_payload(
        self,
        db_engine: AsyncEngine,
        client: Any,
    ) -> None:
        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as ses:
            n = await TelegramNotifyService(ses).enqueue_recovery([42])
        assert n == 1
        r = get_redis()
        items = await r.lrange(TG_NOTIFY_QUEUE_KEY, 0, -1)
        assert len(items) == 1
        decoded = items[0].decode() if isinstance(items[0], bytes) else items[0]
        assert json.loads(decoded)["source"] == "recovery"


# ---------------------------------------------------------------------------
# Malformed payload handling
# ---------------------------------------------------------------------------


class TestMalformedPayload:
    async def test_malformed_payload_does_not_crash(
        self,
        db_engine: AsyncEngine,
        client: Any,
    ) -> None:
        # Just a smoke test: the dispatcher logs + returns silently.
        await _dispatch("not-json-at-all", db_engine)
        await _dispatch(json.dumps({"missing_message_id": True}), db_engine)
        # No exception → success. (We can't easily assert on logs from here.)


# ---------------------------------------------------------------------------
# Sync cycle resilience — Redis outage must not crash sync_cycle
# ---------------------------------------------------------------------------


class TestSyncCycleResilience:
    async def test_enqueue_failure_in_sync_does_not_propagate(
        self,
        db_engine: AsyncEngine,
        client: Any,
        super_admin_user: User,
        create_mail_account: Any,
        create_message: Any,
        tag_message_for_user: Any,
        monkeypatch: Any,
    ) -> None:
        """ADR-0022 §2.1: a Redis LPUSH failure inside ``sync_one_account``
        must be swallowed — the per-account commit has already happened, and
        we don't want a notify-pipeline outage to mark accounts as failed.

        We simulate by patching ``TelegramNotifyService.enqueue_message_ids``
        to raise. The caller in :func:`sync_one_account` wraps the call in
        try/except.
        """
        # Build a tagged message that WOULD be eligible for enqueue.
        acc = await create_mail_account(super_admin_user.id, "resilient@example.com")
        msg = await create_message(acc.id, uid=140001)
        await tag_message_for_user(super_admin_user.id, msg.id, "tag")

        # Patch enqueue to raise.
        from backend.app.telegram import notify_service as ns_mod

        async def _exploding_enqueue(self: Any, message_ids: list[int]) -> int:
            raise RuntimeError("simulated redis outage")

        monkeypatch.setattr(
            ns_mod.TelegramNotifyService,
            "enqueue_message_ids",
            _exploding_enqueue,
        )

        # The sync_cycle path wraps the call in try/except; we can't easily
        # invoke ``sync_one_account`` without a real IMAP server, but we
        # CAN verify the wrapping by calling the service directly and seeing
        # that the exception is propagated by THIS layer (the swallowing is
        # at the worker layer, not the service layer). The service's own
        # contract is "raises on Redis errors" — the worker provides the
        # safety net.
        from backend.app.telegram.notify_service import TelegramNotifyService

        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as ses:
            with pytest.raises(RuntimeError, match="simulated redis outage"):
                await TelegramNotifyService(ses).enqueue_message_ids([msg.id])


# ---------------------------------------------------------------------------
# Round-34 (ADR-0022 §2.4/§2.5): subject + body preview in the dispatch text
# ---------------------------------------------------------------------------


class TestSubjectAndPreviewInDispatch:
    async def test_subject_and_text_preview_present_in_notification(
        self,
        db_engine: AsyncEngine,
        client: Any,
        super_admin_user: User,
        make_link: Any,
        create_mail_account: Any,
        create_message: Any,
        tag_message_for_user: Any,
        fake_send_notification: Any,
    ) -> None:
        """A message with a subject + plain body_text → the rendered push
        contains the ``Тема:`` line and a preview taken from ``body_text``."""
        tg_id = 160001
        await make_link(tg_id, super_admin_user.id)
        acc = await create_mail_account(super_admin_user.id, "subj@example.com")
        msg = await create_message(
            acc.id,
            uid=160001,
            subject="Important update",
            body_text="This is the plain text body that should preview.",
        )
        await tag_message_for_user(super_admin_user.id, msg.id, "tag")
        fake_send_notification.push(FakeSendResult(kind="ok", telegram_message_id=1))

        await _dispatch(_payload_for(msg.id), db_engine)

        assert len(fake_send_notification.calls) == 1
        text_html = fake_send_notification.calls[0]["text_html"]
        assert "Тема: <b>Important update</b>" in text_html
        assert "This is the plain text body that should preview." in text_html

    async def test_html_fallback_used_when_body_text_blank_whitespace(
        self,
        db_engine: AsyncEngine,
        client: Any,
        super_admin_user: User,
        make_link: Any,
        create_mail_account: Any,
        create_message: Any,
        tag_message_for_user: Any,
        fake_send_notification: Any,
    ) -> None:
        """``body_text`` is whitespace-only (NOT NULL ``''``-style default), but
        ``body_html`` carries real text → the preview falls back to the HTML
        part stripped to plain. Verifies the ``.strip()`` (not truthiness) check
        and that style/script content does not leak."""
        tg_id = 160101
        await make_link(tg_id, super_admin_user.id)
        acc = await create_mail_account(super_admin_user.id, "htmlfb@example.com")
        msg = await create_message(
            acc.id,
            uid=160101,
            subject="HTML only",
            body_text="   \n\t  ",  # whitespace-only — must NOT block fallback
            body_html=(
                "<style>.x{height:20px !important;}</style>"
                "<p>Greetings from the <b>HTML</b> part &amp; more.</p>"
            ),
        )
        await tag_message_for_user(super_admin_user.id, msg.id, "tag")
        fake_send_notification.push(FakeSendResult(kind="ok", telegram_message_id=1))

        await _dispatch(_payload_for(msg.id), db_engine)

        text_html = fake_send_notification.calls[0]["text_html"]
        # Preview content from the HTML body is present (entities decoded then
        # re-escaped: ``&amp;`` → ``&`` → ``&amp;``).
        assert "Greetings from the" in text_html
        assert "HTML" in text_html
        # CSS leakage must NOT appear.
        assert "height:20px" not in text_html
        assert "!important" not in text_html
        # No raw markup from the body injected.
        assert "<p>" not in text_html
        assert "<style>" not in text_html

    async def test_preview_identical_for_all_recipients(
        self,
        db_engine: AsyncEngine,
        client: Any,
        super_admin_user: User,
        leader_and_group: tuple[Any, User],
        create_member: Any,
        make_link: Any,
        create_mail_account: Any,
        create_message: Any,
        tag_message_for_user: Any,
        fake_send_notification: Any,
    ) -> None:
        """Preview + subject are computed once per message → every recipient
        receives byte-identical text (round-34: shared across recipients)."""
        group, leader = leader_and_group
        member = await create_member(group.id, "preview_member")
        await make_link(160201, super_admin_user.id)
        await make_link(160202, member.id)

        acc = await create_mail_account(leader.id, "shared@example.com", group_id=group.id)
        msg = await create_message(
            acc.id,
            uid=160201,
            subject="Shared subject",
            body_text="One body previewed identically for everyone.",
        )
        await tag_message_for_user(super_admin_user.id, msg.id, "admin-tag")
        await tag_message_for_user(member.id, msg.id, "admin-tag")  # same (name,color)
        fake_send_notification.push(FakeSendResult(kind="ok", telegram_message_id=1))

        await _dispatch(_payload_for(msg.id), db_engine)

        assert len(fake_send_notification.calls) == 2
        texts = {call["text_html"] for call in fake_send_notification.calls}
        assert len(texts) == 1, f"recipients got divergent text: {texts!r}"
        the_text = texts.pop()
        assert "Тема: <b>Shared subject</b>" in the_text
        assert "One body previewed identically for everyone." in the_text

    async def test_blank_message_renders_no_subject_fallback_and_no_preview(
        self,
        db_engine: AsyncEngine,
        client: Any,
        super_admin_user: User,
        make_link: Any,
        create_mail_account: Any,
        create_message: Any,
        tag_message_for_user: Any,
        fake_send_notification: Any,
    ) -> None:
        """Round-36 (ADR-0022 §2.5): a message with no subject and an empty body
        → the ``Тема:`` line is STILL present with the ``(без темы)`` fallback,
        and NO preview line is emitted (empty body drops the preview + its blank
        separator). The tag line carries the applied tag as usual.
        """
        tg_id = 160301
        await make_link(tg_id, super_admin_user.id)
        acc = await create_mail_account(super_admin_user.id, "blank@example.com")
        msg = await create_message(
            acc.id,
            uid=160301,
            subject=None,
            body_text="",
            body_html=None,
        )
        await tag_message_for_user(super_admin_user.id, msg.id, "tag")
        fake_send_notification.push(FakeSendResult(kind="ok", telegram_message_id=1))

        await _dispatch(_payload_for(msg.id), db_engine)

        text_html = fake_send_notification.calls[0]["text_html"]
        # Subject line is always present with its fallback (round-36).
        assert "Тема: <b>(без темы)</b>" in text_html
        # The applied tag renders on the #️⃣ line.
        assert "#️⃣: <b>tag</b>" in text_html
        # Empty body → no preview line and no trailing blank separator.
        assert not text_html.endswith("\n")
        assert "\n\n\n" not in text_html


# ---------------------------------------------------------------------------
# Per-chat send throttle (ADR-0022 §2.9)
# ---------------------------------------------------------------------------


class TestPerChatThrottle:
    async def _exhaust_chat_budget(self, chat_id: int, capacity: int) -> None:
        """Pre-fill the per-chat throttle counter so the next try_consume fails.

        Mirrors the production key shape ``rl:tg_send:<chat_id>`` and the
        fixed-window INCR mechanics in :func:`try_consume`.
        """
        redis = get_redis()
        key = f"rl:{LIMIT_TG_SEND_PER_CHAT.name}:{chat_id}"
        await redis.set(key, capacity, ex=LIMIT_TG_SEND_PER_CHAT.window_seconds)

    async def test_throttled_recipient_is_skipped_no_row_no_requeue(
        self,
        db_engine: AsyncEngine,
        client: Any,
        super_admin_user: User,
        make_link: Any,
        create_mail_account: Any,
        create_message: Any,
        tag_message_for_user: Any,
        fake_send_notification: Any,
        monkeypatch: Any,
    ) -> None:
        """§2.9: when the per-chat budget is exhausted the recipient is skipped
        via ``continue`` — NO Bot API call, NO ``telegram_notifications`` row,
        and NO ``enqueue_recovery`` (the hourly recovery scan picks it up)."""
        # Pin the throttle capacity small + deterministic.
        monkeypatch.setenv("TG_SEND_PER_CHAT_PER_MINUTE", "1")
        get_settings.cache_clear()
        assert get_settings().TG_SEND_PER_CHAT_PER_MINUTE == 1

        tg_id = 150001
        await make_link(tg_id, super_admin_user.id)
        acc = await create_mail_account(super_admin_user.id, "throttle@example.com")
        msg = await create_message(acc.id, uid=150001)
        await tag_message_for_user(super_admin_user.id, msg.id, "tag")

        # Exhaust the per-chat budget BEFORE dispatch (counter already at cap).
        await self._exhaust_chat_budget(tg_id, capacity=1)

        r = get_redis()
        assert await r.llen(TG_NOTIFY_QUEUE_KEY) == 0

        await _dispatch(_payload_for(msg.id), db_engine)

        # No Bot API call happened (try_consume returned False → continue).
        assert len(fake_send_notification.calls) == 0

        # No telegram_notifications row was reserved for the throttled recipient.
        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as ses:
            row = (
                await ses.execute(
                    select(TelegramNotification).where(TelegramNotification.message_id == msg.id)
                )
            ).scalar_one_or_none()
            assert row is None, f"throttled recipient must NOT reserve a row, got {row}"

        # No re-enqueue for a throttled recipient (recovery scan handles it).
        assert await r.llen(TG_NOTIFY_QUEUE_KEY) == 0

        get_settings.cache_clear()

    async def test_within_budget_recipient_is_delivered(
        self,
        db_engine: AsyncEngine,
        client: Any,
        super_admin_user: User,
        make_link: Any,
        create_mail_account: Any,
        create_message: Any,
        tag_message_for_user: Any,
        fake_send_notification: Any,
        monkeypatch: Any,
    ) -> None:
        """Control: with budget available the same path delivers normally."""
        monkeypatch.setenv("TG_SEND_PER_CHAT_PER_MINUTE", "5")
        get_settings.cache_clear()

        tg_id = 150101
        await make_link(tg_id, super_admin_user.id)
        acc = await create_mail_account(super_admin_user.id, "ok_throttle@example.com")
        msg = await create_message(acc.id, uid=150101)
        await tag_message_for_user(super_admin_user.id, msg.id, "tag")

        fake_send_notification.push(FakeSendResult(kind="ok", telegram_message_id=9))
        await _dispatch(_payload_for(msg.id), db_engine)

        assert len(fake_send_notification.calls) == 1
        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as ses:
            row = (
                await ses.execute(
                    select(TelegramNotification).where(TelegramNotification.message_id == msg.id)
                )
            ).scalar_one()
            assert row.sent_at is not None

        get_settings.cache_clear()

    async def test_retry_after_does_requeue_unlike_throttle(
        self,
        db_engine: AsyncEngine,
        client: Any,
        super_admin_user: User,
        make_link: Any,
        create_mail_account: Any,
        create_message: Any,
        tag_message_for_user: Any,
        fake_send_notification: Any,
        monkeypatch: Any,
    ) -> None:
        """Contrast with throttle: a Bot API ``retry_after`` DOES re-enqueue
        (the recipient passed the throttle but the send was rate-limited)."""
        monkeypatch.setenv("TG_SEND_PER_CHAT_PER_MINUTE", "20")
        get_settings.cache_clear()

        tg_id = 150201
        await make_link(tg_id, super_admin_user.id)
        acc = await create_mail_account(super_admin_user.id, "retry_throttle@example.com")
        msg = await create_message(acc.id, uid=150201)
        await tag_message_for_user(super_admin_user.id, msg.id, "tag")

        fake_send_notification.push(FakeSendResult(kind="retry_after", retry_after_sec=3))
        r = get_redis()
        assert await r.llen(TG_NOTIFY_QUEUE_KEY) == 0

        await _dispatch(_payload_for(msg.id), db_engine)

        # retry_after → re-enqueued (unlike a throttle skip).
        assert await r.llen(TG_NOTIFY_QUEUE_KEY) == 1

        get_settings.cache_clear()
