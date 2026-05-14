"""ADR-0022 §2.8 — recovery scan.

The recovery scan must re-enqueue ``messages`` that:
- have at least one ``message_tags`` row,
- have NO ``telegram_notifications`` row yet,
- were ``fetched_at`` within the lookback window.

Messages older than the window are skipped.
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
        create_mail_account: Any,
        create_message: Any,
        tag_message_for_user: Any,
    ) -> None:
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
        create_mail_account: Any,
        create_message: Any,
        tag_message_for_user: Any,
    ) -> None:
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
        create_mail_account: Any,
        create_message: Any,
        tag_message_for_user: Any,
    ) -> None:
        acc = await create_mail_account(super_admin_user.id, "rec3@example.com")
        msg = await create_message(acc.id, uid=130003)
        await tag_message_for_user(super_admin_user.id, msg.id, "tag")

        # Pretend a recipient was already reserved.
        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as ses, ses.begin():
            await TelegramNotificationsRepo(ses).try_reserve(
                message_id=msg.id, user_id=super_admin_user.id
            )

        ids = await _list_recovery(db_engine)
        assert msg.id not in ids

    async def test_message_without_tags_is_skipped(
        self,
        db_engine: AsyncEngine,
        client: Any,
        super_admin_user: Any,
        create_mail_account: Any,
        create_message: Any,
    ) -> None:
        acc = await create_mail_account(super_admin_user.id, "rec4@example.com")
        msg = await create_message(acc.id, uid=130004)
        # No tag → recovery scan ignores.

        ids = await _list_recovery(db_engine)
        assert msg.id not in ids
