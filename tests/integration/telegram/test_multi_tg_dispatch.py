"""ADR-0024 §7 (Sprint A) — dispatch to ALL live chats of a multi-linked user.

Item C of the Sprint-A QA scope. With the ``UNIQUE(user_id)`` constraint on
``telegram_links`` gone, ``list_recipients_for_message`` yields one row per
live chat, and the dispatcher sends to each:

- a user with TWO live links → recipient SQL returns TWO rows → dispatch makes
  TWO Bot API sends (mocked) + TWO ``telegram_notifications`` rows;
- a chat that returns ``dead`` (403 / chat_not_found) is mark_dead-ed, but the
  user's OTHER live chat still receives;
- a link with ``dead_at`` set is excluded from the recipient SQL entirely
  (``tl.dead_at IS NULL``), so a pre-dead chat never gets contacted.

The Bot API is mocked via ``fake_send_notification``; everything else runs
against live Postgres + Redis.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from backend.app.telegram.notify_service import TelegramNotifyService
from shared.models import TelegramLink, TelegramNotification, User
from tests.integration.telegram.conftest import FakeSendResult

pytestmark = pytest.mark.integration


def _payload_for(message_id: int) -> str:
    return json.dumps({"v": 1, "message_id": int(message_id), "source": "sync"})


async def _dispatch(payload: str, db_engine: AsyncEngine) -> None:
    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with factory() as s, s.begin():
        await TelegramNotifyService(s).dispatch_one_payload(payload)


class TestDeliverToAllLiveChats:
    async def test_two_live_links_one_user_dispatches_two_notifications(
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
        chat_a, chat_b = 310001, 310002
        await make_link(chat_a, super_admin_user.id)
        await make_link(chat_b, super_admin_user.id)
        acc = await create_mail_account(super_admin_user.id, "multidisp@example.com")
        msg = await create_message(acc.id, uid=310001)
        await tag_message_for_user(super_admin_user.id, msg.id, "VIP")
        fake_send_notification.push(FakeSendResult(kind="ok", telegram_message_id=1))

        await _dispatch(_payload_for(msg.id), db_engine)

        # Both chats contacted exactly once each.
        chat_ids = sorted(call["chat_id"] for call in fake_send_notification.calls)
        assert chat_ids == [chat_a, chat_b]

        # Two notification rows, one per chat, both sent.
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
            assert {r.telegram_user_id for r in rows} == {chat_a, chat_b}
            assert all(r.sent_at is not None for r in rows)

    async def test_dead_one_chat_other_still_receives(
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
        """One chat returns ``dead`` → mark_dead that chat ONLY; the sibling chat
        of the same user still gets its notification (mark_dead is per-chat)."""
        dead_chat, live_chat = 310101, 310102
        # Order links so the recipient SQL processes dead_chat first (created
        # earlier → ``ORDER BY created_at`` is desc in list_* but the dispatcher
        # iterates the SQL order). We make BOTH and assert on outcomes, not order.
        await make_link(dead_chat, super_admin_user.id)
        await make_link(live_chat, super_admin_user.id)
        acc = await create_mail_account(super_admin_user.id, "deadlive@example.com")
        msg = await create_message(acc.id, uid=310101)
        await tag_message_for_user(super_admin_user.id, msg.id, "tag")

        # The fake returns ``dead`` for the FIRST call and ``ok`` afterwards.
        # We can't guarantee which chat is first, so script per-chat via a
        # custom recorder behaviour: push dead then ok; the dead one gets
        # mark_dead, the other is delivered.
        fake_send_notification.push(
            FakeSendResult(kind="dead", detail="Forbidden: bot was blocked"),
            FakeSendResult(kind="ok", telegram_message_id=7),
        )

        await _dispatch(_payload_for(msg.id), db_engine)

        # Both chats were attempted (one dead, one ok).
        assert len(fake_send_notification.calls) == 2
        contacted = {call["chat_id"] for call in fake_send_notification.calls}
        assert contacted == {dead_chat, live_chat}

        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as ses:
            # Exactly one link is now dead.
            links = (
                (
                    await ses.execute(
                        select(TelegramLink).where(TelegramLink.user_id == super_admin_user.id)
                    )
                )
                .scalars()
                .all()
            )
            dead = [link for link in links if link.dead_at is not None]
            alive = [link for link in links if link.dead_at is None]
            assert len(dead) == 1
            assert len(alive) == 1

            # The ok chat has a sent row; the dead chat has a row with sent_at NULL.
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
            by_chat = {r.telegram_user_id: r for r in rows}
            # Both chats reserved a row (dead keeps row as audit marker).
            assert set(by_chat) == {dead_chat, live_chat}
            assert sum(1 for r in rows if r.sent_at is not None) == 1
            assert sum(1 for r in rows if r.sent_at is None) == 1

    async def test_pre_dead_link_excluded_from_recipients(
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
        """A link with ``dead_at`` already set is filtered out by the recipient
        SQL (``tl.dead_at IS NULL``) — only the live chat is contacted."""
        dead_chat, live_chat = 310201, 310202
        await make_link(dead_chat, super_admin_user.id, dead=True)
        await make_link(live_chat, super_admin_user.id)
        acc = await create_mail_account(super_admin_user.id, "predead@example.com")
        msg = await create_message(acc.id, uid=310201)
        await tag_message_for_user(super_admin_user.id, msg.id, "tag")
        fake_send_notification.push(FakeSendResult(kind="ok", telegram_message_id=1))

        await _dispatch(_payload_for(msg.id), db_engine)

        contacted = {call["chat_id"] for call in fake_send_notification.calls}
        assert contacted == {live_chat}, "dead link must not be a recipient"

        # Only the live chat produced a notification row.
        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as ses:
            n = int(
                (
                    await ses.execute(
                        select(func.count())
                        .select_from(TelegramNotification)
                        .where(TelegramNotification.message_id == msg.id)
                    )
                ).scalar_one()
            )
            assert n == 1
