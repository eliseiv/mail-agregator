"""ADR-0024 §7 (round-35, Sprint A) — recovery at per-CHAT granularity.

Item D of the Sprint-A QA scope. Round-33 made recovery per-recipient
(``(message_id, user_id)``); ADR-0024 §7 tightens it to per-chat
(``(message_id, telegram_user_id)``) so a SINGLE user with two links whose
chat A was delivered but chat B was skipped (throttle → no row) is still
re-enqueued.

- delivered to chat A only (one of the user's two links) → recovery RETURNS the
  message (chat B still missing a row);
- both chats delivered → recovery does NOT return it;
- invariants preserved: ``internal_date >= tl.created_at`` first-link guard,
  ``dead_at IS NULL``, opt-out, and the ``TG_NOTIFY_ALL_MESSAGES`` predicate.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from backend.app.repositories.telegram_notifications import (
    TelegramNotificationsRepo,
)
from shared.models import Message, User

pytestmark = pytest.mark.integration


async def _recovery(db_engine: AsyncEngine, *, window_hours: int = 24) -> list[int]:
    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with factory() as ses:
        return await TelegramNotificationsRepo(ses).list_missing_for_recovery(
            window_hours=window_hours, limit=100
        )


async def _reserve(
    db_engine: AsyncEngine, *, message_id: int, user_id: int, telegram_user_id: int
) -> None:
    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with factory() as ses, ses.begin():
        await TelegramNotificationsRepo(ses).try_reserve(
            message_id=message_id, user_id=user_id, telegram_user_id=telegram_user_id
        )


class TestPerChatRecoverySingleUser:
    async def test_one_chat_delivered_other_skipped_message_is_recovered(
        self,
        db_engine: AsyncEngine,
        client: Any,
        super_admin_user: User,
        make_link: Any,
        create_mail_account: Any,
        create_message: Any,
        set_tg_notify_all: Callable[[bool], None],
    ) -> None:
        """One user, two links. Chat A delivered (row); chat B skipped (no row,
        e.g. throttled). Recovery MUST return the message for chat B."""
        set_tg_notify_all(True)
        chat_a, chat_b = 320001, 320002
        await make_link(chat_a, super_admin_user.id)
        await make_link(chat_b, super_admin_user.id)
        acc = await create_mail_account(super_admin_user.id, "rec_a@example.com")
        msg = await create_message(acc.id, uid=320001)

        # Chat A delivered; chat B left undelivered (throttle skip leaves no row).
        await _reserve(
            db_engine, message_id=msg.id, user_id=super_admin_user.id, telegram_user_id=chat_a
        )

        ids = await _recovery(db_engine)
        assert msg.id in ids, "per-chat gap: chat B undelivered must re-enqueue the message"

    async def test_both_chats_delivered_message_not_recovered(
        self,
        db_engine: AsyncEngine,
        client: Any,
        super_admin_user: User,
        make_link: Any,
        create_mail_account: Any,
        create_message: Any,
        set_tg_notify_all: Callable[[bool], None],
    ) -> None:
        set_tg_notify_all(True)
        chat_a, chat_b = 320101, 320102
        await make_link(chat_a, super_admin_user.id)
        await make_link(chat_b, super_admin_user.id)
        acc = await create_mail_account(super_admin_user.id, "rec_both@example.com")
        msg = await create_message(acc.id, uid=320101)

        await _reserve(
            db_engine, message_id=msg.id, user_id=super_admin_user.id, telegram_user_id=chat_a
        )
        await _reserve(
            db_engine, message_id=msg.id, user_id=super_admin_user.id, telegram_user_id=chat_b
        )

        ids = await _recovery(db_engine)
        assert msg.id not in ids, "all chats delivered → not recovered"

    async def test_dead_sibling_chat_does_not_keep_message_in_recovery(
        self,
        db_engine: AsyncEngine,
        client: Any,
        super_admin_user: User,
        make_link: Any,
        create_mail_account: Any,
        create_message: Any,
        set_tg_notify_all: Callable[[bool], None],
    ) -> None:
        """If chat A is delivered and chat B is DEAD (``dead_at`` set), recovery
        must NOT return the message — a dead chat is not a pending recipient."""
        set_tg_notify_all(True)
        chat_a, chat_b_dead = 320201, 320202
        await make_link(chat_a, super_admin_user.id)
        await make_link(chat_b_dead, super_admin_user.id, dead=True)
        acc = await create_mail_account(super_admin_user.id, "rec_dead@example.com")
        msg = await create_message(acc.id, uid=320201)

        await _reserve(
            db_engine, message_id=msg.id, user_id=super_admin_user.id, telegram_user_id=chat_a
        )

        ids = await _recovery(db_engine)
        assert msg.id not in ids, "dead sibling chat is not a pending recipient"

    async def test_first_link_guard_excludes_pre_link_message_for_both_chats(
        self,
        db_engine: AsyncEngine,
        client: Any,
        super_admin_user: User,
        make_link: Any,
        create_mail_account: Any,
        create_message: Any,
        set_tg_notify_all: Callable[[bool], None],
    ) -> None:
        """``internal_date < tl.created_at`` excludes the message for chats that
        were linked after the mail arrived — held even with multiple links."""
        set_tg_notify_all(True)
        await make_link(320301, super_admin_user.id)
        await make_link(320302, super_admin_user.id)
        acc = await create_mail_account(super_admin_user.id, "rec_old@example.com")
        old_date = datetime.now(UTC) - timedelta(hours=2)
        msg = await create_message(acc.id, uid=320301, internal_date=old_date)

        ids = await _recovery(db_engine)
        assert msg.id not in ids

    async def test_opt_out_user_not_recovered_for_any_chat(
        self,
        db_engine: AsyncEngine,
        client: Any,
        super_admin_user: User,
        make_link: Any,
        create_mail_account: Any,
        create_message: Any,
        set_tg_notify_all: Callable[[bool], None],
    ) -> None:
        set_tg_notify_all(True)
        await make_link(320401, super_admin_user.id)
        await make_link(320402, super_admin_user.id)
        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as ses, ses.begin():
            from backend.app.repositories.user_settings import UserSettingsRepo

            await UserSettingsRepo(ses).upsert_tg_notifications_enabled(
                user_id=super_admin_user.id, enabled=False
            )
        acc = await create_mail_account(super_admin_user.id, "rec_optout@example.com")
        msg = await create_message(acc.id, uid=320401)

        ids = await _recovery(db_engine)
        assert msg.id not in ids

    async def test_window_cutoff_excludes_old_message(
        self,
        db_engine: AsyncEngine,
        client: Any,
        super_admin_user: User,
        make_link: Any,
        create_mail_account: Any,
        create_message: Any,
        set_tg_notify_all: Callable[[bool], None],
    ) -> None:
        set_tg_notify_all(True)
        await make_link(320501, super_admin_user.id)
        await make_link(320502, super_admin_user.id)
        acc = await create_mail_account(super_admin_user.id, "rec_win@example.com")
        msg = await create_message(acc.id, uid=320501)

        old = datetime.now(UTC) - timedelta(hours=48)
        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as ses, ses.begin():
            await ses.execute(update(Message).where(Message.id == msg.id).values(fetched_at=old))

        ids = await _recovery(db_engine, window_hours=24)
        assert msg.id not in ids
