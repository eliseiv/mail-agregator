"""ADR-0040 x ADR-0022 §2.1 — global tags change the flag=false notify surface.

Behaviour-change lock (S4-A task 3). Before ADR-0040 the builtin catalogue was
PER-USER (lazy-created on login), so the worker hook ``apply_tags_to_message``
attached a builtin tag only for a visible user who had already logged in. A
message that no such user had tagged got ``applied == 0`` and — under
``TG_NOTIFY_ALL_MESSAGES=false`` (the "tagged-only" mode:
``worker/app/sync_cycle.py`` line 330 gates the enqueue on ``applied > 0``; the
recipient SQL appends the ``EXISTS(message_tags)`` predicate via
``backend/app/repositories/telegram_notifications.py`` ``_tag_predicate``) —
produced NO Telegram notification.

After ADR-0040 the builtin catalogue is GLOBAL (``tags.user_id IS NULL``, seeded
at startup) and ``APPLY_TAGS_TO_MESSAGE`` gained the ``t.user_id IS NULL`` branch
(``backend/app/tags/sql.py`` line 206) so a global tag auto-attaches to EVERY
matching message on ANY mailbox, independently of any login. Consequently, under
flag=false, a message that matches a global rule now has ``applied > 0`` and DOES
resolve a linked+visible recipient where before it would not. This is the
ADR-0040-approved behaviour change; it is locked here with the REAL apply SQL +
REAL recipient SQL (never a mock of our own code):

- flag=false + a matching GLOBAL tag → the hook applies it (``applied > 0``) AND
  the linked, visible recipient resolves (the tagged-only predicate is satisfied
  by the global tag row);
- flag=false + NO matching tag (control) → ``applied == 0`` AND NO recipient —
  proving the resolution above is caused by the global tag, not the flag alone.

The default PRODUCTION flag is ``TG_NOTIFY_ALL_MESSAGES=true``
(``shared/config.py``), under which every visible message notifies regardless of
tags, so this global-tag effect is only observable in the opt-in tagged-only
mode. A twin assertion confirms the flag=true path is unaffected either way.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from backend.app.repositories.telegram_notifications import TelegramNotificationsRepo
from backend.app.tags.service import TagsService
from shared.models import Message, Tag, TagRule, User

pytestmark = pytest.mark.integration


async def _make_global_tag(db_engine: AsyncEngine, *, name: str, pattern: str) -> int:
    """A GLOBAL builtin tag (``user_id IS NULL``) with a single subject rule."""
    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with factory() as ses, ses.begin():
        tag = Tag(user_id=None, name=name, color="#2563eb", is_builtin=True, match_mode="any")
        ses.add(tag)
        await ses.flush()
        ses.add(TagRule(tag_id=tag.id, type="subject_contains", pattern=pattern))
        await ses.flush()
        return int(tag.id)


async def _apply_worker_hook(db_engine: AsyncEngine, *, message: Message, account_id: int) -> int:
    """Run the REAL ``apply_tags_to_message`` (the worker's post-insert hook)."""
    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with factory() as ses, ses.begin():
        return await TagsService(ses).apply_tags_to_message(
            message=message, mail_account_id=account_id
        )


async def _recipients(db_engine: AsyncEngine, message_id: int) -> list[int]:
    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with factory() as ses:
        rows = await TelegramNotificationsRepo(ses).list_recipients_for_message(
            message_id=message_id
        )
    return [r.user_id for r in rows]


class TestGlobalTagFlagOff:
    async def test_matching_global_tag_makes_flagoff_message_notify(
        self,
        db_engine: AsyncEngine,
        client: Any,  # forces app lifespan (harmless seed of crm-service etc.)
        super_admin_user: User,
        make_link: Any,
        create_mail_account: Any,
        create_message: Any,
        set_tg_notify_all: Callable[[bool], None],
    ) -> None:
        set_tg_notify_all(False)
        # Link FIRST so ``m.internal_date >= tl.created_at`` admits the message.
        await make_link(240001, super_admin_user.id)
        acc = await create_mail_account(super_admin_user.id, "gvol-on@example.com")
        msg = await create_message(acc.id, uid=240001, subject="Quarterly Globalword Report")

        # Real global builtin tag matching the subject.
        tag_id = await _make_global_tag(db_engine, name="G-Vol", pattern="Globalword")

        applied = await _apply_worker_hook(db_engine, message=msg, account_id=acc.id)
        # The behaviour change: a global tag attaches with NO per-user tag at all.
        assert applied == 1, "the global tag must auto-attach (applied>0)"

        recipients = await _recipients(db_engine, msg.id)
        assert (
            super_admin_user.id in recipients
        ), "under flag=false a global-tagged message now resolves a recipient"
        del tag_id

    async def test_unmatched_message_stays_silent_flagoff(
        self,
        db_engine: AsyncEngine,
        client: Any,
        super_admin_user: User,
        make_link: Any,
        create_mail_account: Any,
        create_message: Any,
        set_tg_notify_all: Callable[[bool], None],
    ) -> None:
        """Control: with the SAME global tag present but a NON-matching subject,
        the hook applies nothing and — under flag=false — no recipient resolves.
        Proves the notification above is caused by the tag, not the flag alone."""
        set_tg_notify_all(False)
        await make_link(240101, super_admin_user.id)
        acc = await create_mail_account(super_admin_user.id, "gvol-off@example.com")
        msg = await create_message(acc.id, uid=240101, subject="nothing interesting here")

        await _make_global_tag(db_engine, name="G-Vol-Ctl", pattern="Globalword")

        applied = await _apply_worker_hook(db_engine, message=msg, account_id=acc.id)
        assert applied == 0, "no rule matches → nothing attaches"

        recipients = await _recipients(db_engine, msg.id)
        assert (
            super_admin_user.id not in recipients
        ), "an untagged message stays silent in tagged-only mode"


class TestGlobalTagFlagOn:
    async def test_flag_on_notifies_regardless_of_global_tag(
        self,
        db_engine: AsyncEngine,
        client: Any,
        super_admin_user: User,
        make_link: Any,
        create_mail_account: Any,
        create_message: Any,
        set_tg_notify_all: Callable[[bool], None],
    ) -> None:
        """Under the DEFAULT flag=true, a message notifies whether or not a global
        tag matched — the global-tag effect is confined to the tagged-only mode."""
        set_tg_notify_all(True)
        await make_link(240201, super_admin_user.id)
        acc = await create_mail_account(super_admin_user.id, "gvol-onmode@example.com")
        # Deliberately NON-matching subject: flag=true ignores the tag predicate.
        msg = await create_message(acc.id, uid=240201, subject="no match at all")
        await _make_global_tag(db_engine, name="G-Vol-On", pattern="Globalword")

        applied = await _apply_worker_hook(db_engine, message=msg, account_id=acc.id)
        assert applied == 0

        recipients = await _recipients(db_engine, msg.id)
        assert super_admin_user.id in recipients, "flag=true notifies untagged messages too"
