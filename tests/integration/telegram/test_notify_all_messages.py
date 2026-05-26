"""ADR-0022 §2.1 / §2.2 / §2.8 (round-31 + round-33) — TG_NOTIFY_ALL_MESSAGES.

Covers (spec items C + D):

C. ``list_recipients_for_message`` under both flag modes:
   - flag=true: an UNtagged, visible+linked message DOES resolve a recipient.
   - flag=false: same untagged message resolves NO recipient; tagging it makes
     the recipient reappear.
   - In BOTH modes the invariants hold: visibility (super_admin / group / owner),
     ``dead_at IS NULL``, ``internal_date >= tl.created_at``, opt-out
     (``tg_notifications_enabled=false``).

D. ``list_missing_for_recovery`` per-recipient gap (round-33):
   - message visible to A and B, A delivered (row exists) but B not → recovery
     STILL returns the message id (for B).
   - fully delivered (rows for both A and B) → NOT returned.
   - respects window cutoff + the conditional tag predicate under the flag.

The flag is flipped via the ``set_tg_notify_all`` fixture, which sets the env
var and clears the ``get_settings`` lru-cache so the repository (which reads
``get_settings().TG_NOTIFY_ALL_MESSAGES`` at query time) observes the change.
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

# ``set_tg_notify_all`` is provided by tests/integration/telegram/conftest.py.


async def _recipients(db_engine: AsyncEngine, message_id: int) -> list[int]:
    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with factory() as ses:
        rows = await TelegramNotificationsRepo(ses).list_recipients_for_message(
            message_id=message_id
        )
    return [r.user_id for r in rows]


async def _recovery(db_engine: AsyncEngine, *, window_hours: int = 24) -> list[int]:
    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with factory() as ses:
        return await TelegramNotificationsRepo(ses).list_missing_for_recovery(
            window_hours=window_hours, limit=100
        )


async def _reserve(db_engine: AsyncEngine, *, message_id: int, user_id: int) -> None:
    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with factory() as ses, ses.begin():
        await TelegramNotificationsRepo(ses).try_reserve(message_id=message_id, user_id=user_id)


# ---------------------------------------------------------------------------
# C. list_recipients_for_message — flag on vs off
# ---------------------------------------------------------------------------


class TestRecipientsFlagOn:
    async def test_untagged_message_resolves_recipient_when_flag_on(
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
        await make_link(210001, super_admin_user.id)
        acc = await create_mail_account(super_admin_user.id, "allon@example.com")
        msg = await create_message(acc.id, uid=210001)
        # NO tag applied.
        recipients = await _recipients(db_engine, msg.id)
        assert super_admin_user.id in recipients


class TestRecipientsFlagOff:
    async def test_untagged_message_resolves_no_recipient_when_flag_off(
        self,
        db_engine: AsyncEngine,
        client: Any,
        super_admin_user: User,
        make_link: Any,
        create_mail_account: Any,
        create_message: Any,
        set_tg_notify_all: Callable[[bool], None],
    ) -> None:
        set_tg_notify_all(False)
        await make_link(210101, super_admin_user.id)
        acc = await create_mail_account(super_admin_user.id, "alloff@example.com")
        msg = await create_message(acc.id, uid=210101)
        # NO tag → tagged-only mode excludes it.
        recipients = await _recipients(db_engine, msg.id)
        assert super_admin_user.id not in recipients

    async def test_tagged_message_resolves_recipient_when_flag_off(
        self,
        db_engine: AsyncEngine,
        client: Any,
        super_admin_user: User,
        make_link: Any,
        create_mail_account: Any,
        create_message: Any,
        tag_message_for_user: Any,
        set_tg_notify_all: Callable[[bool], None],
    ) -> None:
        set_tg_notify_all(False)
        await make_link(210201, super_admin_user.id)
        acc = await create_mail_account(super_admin_user.id, "tagoff@example.com")
        msg = await create_message(acc.id, uid=210201)
        await tag_message_for_user(super_admin_user.id, msg.id, "tag")
        recipients = await _recipients(db_engine, msg.id)
        assert super_admin_user.id in recipients


# ---------------------------------------------------------------------------
# C. invariants hold in BOTH modes
# ---------------------------------------------------------------------------


class TestRecipientInvariantsBothModes:
    @pytest.mark.parametrize("flag", [True, False])
    async def test_dead_link_never_resolves(
        self,
        flag: bool,
        db_engine: AsyncEngine,
        client: Any,
        super_admin_user: User,
        make_link: Any,
        create_mail_account: Any,
        create_message: Any,
        tag_message_for_user: Any,
        set_tg_notify_all: Callable[[bool], None],
    ) -> None:
        set_tg_notify_all(flag)
        await make_link(210301, super_admin_user.id, dead=True)
        acc = await create_mail_account(super_admin_user.id, f"dead{int(flag)}@example.com")
        msg = await create_message(acc.id, uid=210301)
        # Tag it so the flag-off path would otherwise include it.
        await tag_message_for_user(super_admin_user.id, msg.id, "tag")
        recipients = await _recipients(db_engine, msg.id)
        assert super_admin_user.id not in recipients

    @pytest.mark.parametrize("flag", [True, False])
    async def test_message_before_link_created_at_excluded(
        self,
        flag: bool,
        db_engine: AsyncEngine,
        client: Any,
        super_admin_user: User,
        make_link: Any,
        create_mail_account: Any,
        create_message: Any,
        tag_message_for_user: Any,
        set_tg_notify_all: Callable[[bool], None],
    ) -> None:
        set_tg_notify_all(flag)
        # Link created now; message internal_date predates it by an hour.
        await make_link(210401, super_admin_user.id)
        acc = await create_mail_account(super_admin_user.id, f"old{int(flag)}@example.com")
        old_date = datetime.now(UTC) - timedelta(hours=1)
        msg = await create_message(acc.id, uid=210401, internal_date=old_date)
        await tag_message_for_user(super_admin_user.id, msg.id, "tag")
        recipients = await _recipients(db_engine, msg.id)
        # internal_date < tl.created_at → first-link backfill guard excludes.
        assert super_admin_user.id not in recipients

    @pytest.mark.parametrize("flag", [True, False])
    async def test_opt_out_excluded(
        self,
        flag: bool,
        db_engine: AsyncEngine,
        client: Any,
        leader_and_group: tuple[Any, User],
        create_member: Any,
        make_link: Any,
        create_mail_account: Any,
        create_message: Any,
        tag_message_for_user: Any,
        set_tg_notify_all: Callable[[bool], None],
    ) -> None:
        set_tg_notify_all(flag)
        group, leader = leader_and_group
        member = await create_member(group.id, f"optout{int(flag)}")
        await make_link(210501 if flag else 210502, member.id)

        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as ses, ses.begin():
            from backend.app.repositories.user_settings import UserSettingsRepo

            await UserSettingsRepo(ses).upsert_tg_notifications_enabled(
                user_id=member.id, enabled=False
            )

        acc = await create_mail_account(
            leader.id, f"optoutacc{int(flag)}@example.com", group_id=group.id
        )
        msg = await create_message(acc.id, uid=210501 if flag else 210502)
        await tag_message_for_user(member.id, msg.id, "tag")
        recipients = await _recipients(db_engine, msg.id)
        assert member.id not in recipients

    @pytest.mark.parametrize("flag", [True, False])
    async def test_group_visibility_member_included(
        self,
        flag: bool,
        db_engine: AsyncEngine,
        client: Any,
        leader_and_group: tuple[Any, User],
        create_member: Any,
        make_link: Any,
        create_mail_account: Any,
        create_message: Any,
        tag_message_for_user: Any,
        set_tg_notify_all: Callable[[bool], None],
    ) -> None:
        set_tg_notify_all(flag)
        group, leader = leader_and_group
        member = await create_member(group.id, f"vis{int(flag)}")
        await make_link(210601 if flag else 210602, member.id)
        acc = await create_mail_account(
            leader.id, f"visacc{int(flag)}@example.com", group_id=group.id
        )
        msg = await create_message(acc.id, uid=210601 if flag else 210602)
        # Tag so flag-off mode includes; flag-on mode includes regardless.
        await tag_message_for_user(member.id, msg.id, "tag")
        recipients = await _recipients(db_engine, msg.id)
        assert member.id in recipients


# ---------------------------------------------------------------------------
# D. list_missing_for_recovery — per-recipient gap (round-33)
# ---------------------------------------------------------------------------


class TestRecoveryPerRecipient:
    async def test_partial_delivery_still_returns_message_for_undelivered(
        self,
        db_engine: AsyncEngine,
        client: Any,
        super_admin_user: User,
        leader_and_group: tuple[Any, User],
        create_member: Any,
        make_link: Any,
        create_mail_account: Any,
        create_message: Any,
        set_tg_notify_all: Callable[[bool], None],
    ) -> None:
        """Round-33 KEY case: message visible to A (super_admin) and B (member).
        A is delivered (telegram_notifications row exists), B is NOT. The
        recovery scan MUST still return the message id (so B gets re-enqueued).
        """
        set_tg_notify_all(True)  # untagged still eligible — exercises §2.1+§2.8
        group, leader = leader_and_group
        member = await create_member(group.id, "recv_b")
        # A = super_admin, B = member; both linked.
        await make_link(220001, super_admin_user.id)
        await make_link(220002, member.id)
        acc = await create_mail_account(leader.id, "partial@example.com", group_id=group.id)
        msg = await create_message(acc.id, uid=220001)

        # Sanity: both are recipients.
        recipients = await _recipients(db_engine, msg.id)
        assert super_admin_user.id in recipients
        assert member.id in recipients

        # A delivered (row created); B has no row.
        await _reserve(db_engine, message_id=msg.id, user_id=super_admin_user.id)

        ids = await _recovery(db_engine)
        assert msg.id in ids, "per-recipient gap: B undelivered must re-enqueue the message"

    async def test_fully_delivered_message_not_returned(
        self,
        db_engine: AsyncEngine,
        client: Any,
        super_admin_user: User,
        leader_and_group: tuple[Any, User],
        create_member: Any,
        make_link: Any,
        create_mail_account: Any,
        create_message: Any,
        set_tg_notify_all: Callable[[bool], None],
    ) -> None:
        set_tg_notify_all(True)
        group, leader = leader_and_group
        member = await create_member(group.id, "recv_full")
        await make_link(220101, super_admin_user.id)
        await make_link(220102, member.id)
        acc = await create_mail_account(leader.id, "full@example.com", group_id=group.id)
        msg = await create_message(acc.id, uid=220101)

        # Both delivered.
        await _reserve(db_engine, message_id=msg.id, user_id=super_admin_user.id)
        await _reserve(db_engine, message_id=msg.id, user_id=member.id)

        ids = await _recovery(db_engine)
        assert msg.id not in ids, "fully-delivered message must NOT be re-enqueued"

    async def test_recovery_respects_window_cutoff(
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
        await make_link(220201, super_admin_user.id)
        acc = await create_mail_account(super_admin_user.id, "win@example.com")
        msg = await create_message(acc.id, uid=220201)

        # Push fetched_at outside the 24h window.
        old = datetime.now(UTC) - timedelta(hours=48)
        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as ses, ses.begin():
            await ses.execute(update(Message).where(Message.id == msg.id).values(fetched_at=old))

        ids = await _recovery(db_engine, window_hours=24)
        assert msg.id not in ids

    async def test_recovery_respects_tag_predicate_when_flag_off(
        self,
        db_engine: AsyncEngine,
        client: Any,
        super_admin_user: User,
        make_link: Any,
        create_mail_account: Any,
        create_message: Any,
        set_tg_notify_all: Callable[[bool], None],
    ) -> None:
        """flag=false: an untagged message must NOT be returned by recovery even
        though it has a visible, linked, undelivered recipient."""
        set_tg_notify_all(False)
        await make_link(220301, super_admin_user.id)
        acc = await create_mail_account(super_admin_user.id, "tagpred@example.com")
        msg = await create_message(acc.id, uid=220301)
        # No tag → tagged-only recovery excludes it.
        ids = await _recovery(db_engine)
        assert msg.id not in ids

    async def test_recovery_untagged_included_when_flag_on(
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
        await make_link(220401, super_admin_user.id)
        acc = await create_mail_account(super_admin_user.id, "tagpredon@example.com")
        msg = await create_message(acc.id, uid=220401)
        ids = await _recovery(db_engine)
        assert msg.id in ids
