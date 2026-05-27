"""ADR-0022 §2.8 — recovery scan (round-33 per-recipient + round-31 flag).

The recovery scan re-enqueues ``messages`` that have a **visible, linked,
opted-in recipient WITHOUT a** ``telegram_notifications`` row, within the
lookback window. Round-33 made the gap per-recipient (the SQL JOINs the same
recipient logic as §2.2 and requires an active ``telegram_links`` row with
``internal_date >= tl.created_at``). Round-31 made the tag predicate
conditional on ``TG_NOTIFY_ALL_MESSAGES``:

- flag=true (default): an UNtagged message with a linked recipient is included.
- flag=false: only messages with at least one tag are included.

Messages older than the window, or fully delivered, are skipped. Because the
scan now requires a linked recipient, every test here creates a
``telegram_links`` row for the super-admin.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from backend.app.repositories.telegram_notifications import (
    TelegramNotificationsRepo,
)
from backend.app.telegram.notify_service import (
    TG_NOTIFY_QUEUE_KEY,
    TelegramNotifyService,
)
from shared.models import Message
from shared.redis_client import get_redis

pytestmark = pytest.mark.integration


async def _list_recovery(db_engine: AsyncEngine, *, window_hours: int = 24) -> list[int]:
    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with factory() as ses:
        return await TelegramNotificationsRepo(ses).list_missing_for_recovery(
            window_hours=window_hours, limit=100
        )


async def _enqueue(db_engine: AsyncEngine, ids: list[int]) -> int:
    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with factory() as ses:
        return await TelegramNotifyService(ses).enqueue_recovery(ids)


class TestRecoveryScan:
    async def test_message_in_window_with_tags_no_row_is_enqueued(
        self,
        db_engine: AsyncEngine,
        client: Any,
        super_admin_user: Any,
        make_link: Any,
        create_mail_account: Any,
        create_message: Any,
        tag_message_for_user: Any,
    ) -> None:
        # Round-33: recovery requires a linked recipient.
        await make_link(130001, super_admin_user.id)
        acc = await create_mail_account(super_admin_user.id, "rec@example.com")
        msg = await create_message(acc.id, uid=130001)
        await tag_message_for_user(super_admin_user.id, msg.id, "tag")

        ids = await _list_recovery(db_engine)
        assert msg.id in ids

        # Enqueue them.
        await _enqueue(db_engine, ids)
        r = get_redis()
        items = await r.lrange(TG_NOTIFY_QUEUE_KEY, 0, -1)
        # All payloads carry source=recovery.
        for raw in items:
            decoded = raw.decode() if isinstance(raw, bytes) else raw
            payload = json.loads(decoded)
            assert payload["source"] == "recovery"

    async def test_message_outside_window_is_skipped(
        self,
        db_engine: AsyncEngine,
        client: Any,
        super_admin_user: Any,
        make_link: Any,
        create_mail_account: Any,
        create_message: Any,
        tag_message_for_user: Any,
    ) -> None:
        await make_link(130002, super_admin_user.id)
        acc = await create_mail_account(super_admin_user.id, "rec2@example.com")
        msg = await create_message(acc.id, uid=130002)
        await tag_message_for_user(super_admin_user.id, msg.id, "tag")

        # Force fetched_at to be older than the lookback window.
        old = datetime.now(UTC) - timedelta(hours=48)
        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as ses, ses.begin():
            await ses.execute(update(Message).where(Message.id == msg.id).values(fetched_at=old))

        ids = await _list_recovery(db_engine, window_hours=24)
        assert msg.id not in ids

    async def test_message_with_existing_notification_row_is_skipped(
        self,
        db_engine: AsyncEngine,
        client: Any,
        super_admin_user: Any,
        make_link: Any,
        create_mail_account: Any,
        create_message: Any,
        tag_message_for_user: Any,
    ) -> None:
        await make_link(130003, super_admin_user.id)
        acc = await create_mail_account(super_admin_user.id, "rec3@example.com")
        msg = await create_message(acc.id, uid=130003)
        await tag_message_for_user(super_admin_user.id, msg.id, "tag")

        # Pretend a recipient was already reserved (ADR-0024 §6: the key is now
        # per-chat ``(message_id, telegram_user_id)``, so reserve the chat that
        # the single link above resolves to — 130003).
        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as ses, ses.begin():
            await TelegramNotificationsRepo(ses).try_reserve(
                message_id=msg.id, user_id=super_admin_user.id, telegram_user_id=130003
            )

        ids = await _list_recovery(db_engine)
        assert msg.id not in ids

    @pytest.mark.parametrize("flag", [False, True])
    async def test_message_without_tags_included_only_when_flag_on(
        self,
        flag: bool,
        db_engine: AsyncEngine,
        client: Any,
        super_admin_user: Any,
        make_link: Any,
        create_mail_account: Any,
        create_message: Any,
        set_tg_notify_all: Any,
    ) -> None:
        """Round-31: an untagged message is skipped by recovery under
        ``TG_NOTIFY_ALL_MESSAGES=false`` but included under ``true``."""
        set_tg_notify_all(flag)
        await make_link(130040 + int(flag), super_admin_user.id)
        acc = await create_mail_account(super_admin_user.id, f"rec4_{int(flag)}@example.com")
        msg = await create_message(acc.id, uid=130004 + int(flag))
        # No tag applied.

        ids = await _list_recovery(db_engine)
        if flag:
            assert msg.id in ids
        else:
            assert msg.id not in ids
