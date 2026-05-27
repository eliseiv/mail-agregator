"""ADR-0024 §6 (Sprint A) — per-chat idempotency key on ``try_reserve``.

Item B of the Sprint-A QA scope:

- one ``message_id`` reserved for two DIFFERENT ``telegram_user_id`` of the
  SAME user → TWO rows (the user with two links gets one notification row per
  chat);
- re-reserving the SAME ``(message_id, telegram_user_id)`` → ``None`` (per-chat
  dedup), no second row;
- different chats never collide on the same message.

These exercise the repository directly against live Postgres (no mocks) so the
real ``UNIQUE(message_id, telegram_user_id)`` constraint + ON CONFLICT DO
NOTHING semantics are validated end-to-end.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from backend.app.repositories.telegram_notifications import (
    TelegramNotificationsRepo,
)
from shared.models import TelegramNotification, User

pytestmark = pytest.mark.integration


async def _reserve(
    db_engine: AsyncEngine, *, message_id: int, user_id: int, telegram_user_id: int
) -> int | None:
    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with factory() as ses, ses.begin():
        return await TelegramNotificationsRepo(ses).try_reserve(
            message_id=message_id, user_id=user_id, telegram_user_id=telegram_user_id
        )


async def _count_rows(db_engine: AsyncEngine, *, message_id: int) -> int:
    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with factory() as ses:
        return int(
            (
                await ses.execute(
                    select(func.count())
                    .select_from(TelegramNotification)
                    .where(TelegramNotification.message_id == message_id)
                )
            ).scalar_one()
        )


class TestPerChatReserve:
    async def test_same_message_two_chats_same_user_yields_two_rows(
        self,
        db_engine: AsyncEngine,
        client: Any,
        super_admin_user: User,
        make_link: Any,
        create_mail_account: Any,
        create_message: Any,
    ) -> None:
        """ADR-0024 §6: a user with two links → one reserved row per chat."""
        chat_a, chat_b = 300001, 300002
        await make_link(chat_a, super_admin_user.id)
        await make_link(chat_b, super_admin_user.id)
        acc = await create_mail_account(super_admin_user.id, "reserve2@example.com")
        msg = await create_message(acc.id, uid=300001)

        id_a = await _reserve(
            db_engine, message_id=msg.id, user_id=super_admin_user.id, telegram_user_id=chat_a
        )
        id_b = await _reserve(
            db_engine, message_id=msg.id, user_id=super_admin_user.id, telegram_user_id=chat_b
        )

        assert id_a is not None
        assert id_b is not None
        assert id_a != id_b
        assert await _count_rows(db_engine, message_id=msg.id) == 2

        # Each row carries the right chat.
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
            assert all(r.user_id == super_admin_user.id for r in rows)

    async def test_repeat_same_chat_returns_none_and_no_second_row(
        self,
        db_engine: AsyncEngine,
        client: Any,
        super_admin_user: User,
        make_link: Any,
        create_mail_account: Any,
        create_message: Any,
    ) -> None:
        """Re-reserving the SAME ``(message_id, telegram_user_id)`` → ``None``."""
        chat = 300101
        await make_link(chat, super_admin_user.id)
        acc = await create_mail_account(super_admin_user.id, "reservedup@example.com")
        msg = await create_message(acc.id, uid=300101)

        first = await _reserve(
            db_engine, message_id=msg.id, user_id=super_admin_user.id, telegram_user_id=chat
        )
        second = await _reserve(
            db_engine, message_id=msg.id, user_id=super_admin_user.id, telegram_user_id=chat
        )

        assert first is not None
        assert second is None, "per-chat dedup must return None on the second reserve"
        assert await _count_rows(db_engine, message_id=msg.id) == 1

    async def test_different_chats_do_not_conflict(
        self,
        db_engine: AsyncEngine,
        client: Any,
        super_admin_user: User,
        leader_and_group: tuple[Any, User],
        create_member: Any,
        make_link: Any,
        create_mail_account: Any,
        create_message: Any,
    ) -> None:
        """Two DIFFERENT users each reserve the same message for their own chat —
        independent rows, no conflict (the key is per-chat, not per-message)."""
        group, leader = leader_and_group
        member = await create_member(group.id, "reserve_member")
        chat_admin, chat_member = 300201, 300202
        await make_link(chat_admin, super_admin_user.id)
        await make_link(chat_member, member.id)
        acc = await create_mail_account(leader.id, "reserve_grp@example.com", group_id=group.id)
        msg = await create_message(acc.id, uid=300201)

        id_admin = await _reserve(
            db_engine, message_id=msg.id, user_id=super_admin_user.id, telegram_user_id=chat_admin
        )
        id_member = await _reserve(
            db_engine, message_id=msg.id, user_id=member.id, telegram_user_id=chat_member
        )

        assert id_admin is not None
        assert id_member is not None
        assert await _count_rows(db_engine, message_id=msg.id) == 2
