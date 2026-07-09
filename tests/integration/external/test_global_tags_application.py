"""Global-tag APPLICATION semantics (ADR-0040 §1/§3 — the LEFT JOIN in tags/sql.py).

A tag with ``user_id IS NULL`` is GLOBAL: it applies to EVERY message of the
system, regardless of who owns the mailbox. ADR-0040 changed
``APPLY_TAGS_TO_MESSAGE`` to ``LEFT JOIN users`` + a ``t.user_id IS NULL`` branch
so a global tag (which has no owner row) is not dropped by the old INNER JOIN.

This file asserts, against real Postgres and the real SQL (never a mock of our
own code):

- a global tag auto-attaches to a fresh message on ANY mailbox (worker hook
  ``apply_tags_to_message``), and the attach is idempotent
  (``ON CONFLICT (message_id, tag_id) DO NOTHING``);
- ``apply_to_existing_global`` reaches messages across DIFFERENT owners/teams;
- the ADR-0017 matching contract is UNCHANGED for global tags: whole-word,
  case-SENSITIVE, pattern-escaped;
- PERSONAL tag behaviour is a silent-regression guard — a non-super_admin's
  personal tag still only reaches its own messages; a super_admin's personal tag
  still reaches all (round-26). The global-tag work did not change either.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from backend.app.tags.service import TagsService
from shared.models import MailAccount, Message, Tag, TagRule, User

pytestmark = pytest.mark.integration


async def _make_global_tag(
    db_engine: AsyncEngine, *, name: str, rule_type: str, pattern: str, match_mode: str = "any"
) -> int:
    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with factory() as ses, ses.begin():
        tag = Tag(user_id=None, name=name, color="#2563eb", is_builtin=False, match_mode=match_mode)
        ses.add(tag)
        await ses.flush()
        ses.add(TagRule(tag_id=tag.id, type=rule_type, pattern=pattern))
        await ses.flush()
        return int(tag.id)


async def _make_personal_tag(
    db_engine: AsyncEngine, *, user_id: int, name: str, rule_type: str, pattern: str
) -> int:
    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with factory() as ses, ses.begin():
        tag = Tag(user_id=user_id, name=name, color="#2563eb", is_builtin=False, match_mode="any")
        ses.add(tag)
        await ses.flush()
        ses.add(TagRule(tag_id=tag.id, type=rule_type, pattern=pattern))
        await ses.flush()
        return int(tag.id)


async def _tag_ids_on_message(db_engine: AsyncEngine, message_id: int) -> set[int]:
    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with factory() as ses:
        rows = (
            await ses.execute(
                text("SELECT tag_id FROM message_tags WHERE message_id = :m"), {"m": message_id}
            )
        ).all()
    return {int(r[0]) for r in rows}


async def _apply_worker_hook(
    db_engine: AsyncEngine, *, message: Message, account: MailAccount
) -> int:
    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with factory() as ses, ses.begin():
        return await TagsService(ses).apply_tags_to_message(
            message=message, mail_account_id=account.id
        )


class TestGlobalTagWorkerHook:
    async def test_global_tag_attaches_to_any_mailbox(
        self,
        client: Any,  # forces app lifespan (seeds crm-service; harmless)
        db_engine: AsyncEngine,
        super_admin: User,
        make_mail_account: Callable[..., Any],
        make_message: Callable[..., Any],
    ) -> None:
        acc = await make_mail_account(super_admin.id, "gh@example.com")
        msg = await make_message(acc.id, uid=1, subject="Quarterly Report attached")
        tag_id = await _make_global_tag(
            db_engine, name="G-Report", rule_type="subject_contains", pattern="Report"
        )
        applied = await _apply_worker_hook(db_engine, message=msg, account=acc)
        assert applied == 1
        assert tag_id in await _tag_ids_on_message(db_engine, msg.id)

    async def test_worker_hook_is_idempotent(
        self,
        client: Any,
        db_engine: AsyncEngine,
        super_admin: User,
        make_mail_account: Callable[..., Any],
        make_message: Callable[..., Any],
    ) -> None:
        acc = await make_mail_account(super_admin.id, "gi@example.com")
        msg = await make_message(acc.id, uid=1, subject="Report ready")
        await _make_global_tag(
            db_engine, name="G-Idem", rule_type="subject_contains", pattern="Report"
        )
        first = await _apply_worker_hook(db_engine, message=msg, account=acc)
        second = await _apply_worker_hook(db_engine, message=msg, account=acc)
        assert first == 1
        assert second == 0, "re-run must insert 0 rows (ON CONFLICT DO NOTHING)"

    async def test_matching_is_whole_word_case_sensitive(
        self,
        client: Any,
        db_engine: AsyncEngine,
        super_admin: User,
        make_mail_account: Callable[..., Any],
        make_message: Callable[..., Any],
    ) -> None:
        """ADR-0017 §4 matching is unchanged for global tags: pattern ``Ping``
        matches the whole word ``Ping`` but NOT ``ping`` (case) nor ``Pinging``
        (substring-in-word)."""
        acc = await make_mail_account(super_admin.id, "gm@example.com")
        exact = await make_message(acc.id, uid=1, subject="Ping received")
        wrong_case = await make_message(acc.id, uid=2, subject="ping received")
        substring = await make_message(acc.id, uid=3, subject="Pinging now")
        tag_id = await _make_global_tag(
            db_engine, name="G-Ping", rule_type="subject_contains", pattern="Ping"
        )
        for m in (exact, wrong_case, substring):
            await _apply_worker_hook(db_engine, message=m, account=acc)

        assert tag_id in await _tag_ids_on_message(db_engine, exact.id)
        assert tag_id not in await _tag_ids_on_message(db_engine, wrong_case.id)
        assert tag_id not in await _tag_ids_on_message(db_engine, substring.id)


class TestGlobalApplyToExisting:
    async def test_reaches_messages_across_owners(
        self,
        client: Any,
        db_engine: AsyncEngine,
        super_admin: User,
        make_mail_account: Callable[..., Any],
        make_message: Callable[..., Any],
        make_secondary_team_mailbox: Callable[..., Any],
    ) -> None:
        """A global apply-to-existing tags matching messages regardless of the
        owner/team (super_admin short-circuit forces the visibility filter TRUE)."""
        acc_admin = await make_mail_account(super_admin.id, "ga@example.com")
        acc_team = await make_secondary_team_mailbox(
            username="ga_owner", group_name="GA-Team", email="ga-team@example.com"
        )
        m_admin = await make_message(acc_admin.id, uid=1, subject="Global Report a")
        m_team = await make_message(acc_team.id, uid=1, subject="Global Report b")
        tag_id = await _make_global_tag(
            db_engine, name="G-Apply", rule_type="subject_contains", pattern="Report"
        )

        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as ses, ses.begin():
            applied = await TagsService(ses).apply_to_existing_global(tag_id=tag_id)
        assert applied == 2, "global reach tags BOTH owners' matching messages"
        assert tag_id in await _tag_ids_on_message(db_engine, m_admin.id)
        assert tag_id in await _tag_ids_on_message(db_engine, m_team.id)


class TestPersonalTagUnchanged:
    async def test_super_admin_personal_tag_still_reaches_all(
        self,
        client: Any,
        db_engine: AsyncEngine,
        super_admin: User,
        make_mail_account: Callable[..., Any],
        make_message: Callable[..., Any],
        make_secondary_team_mailbox: Callable[..., Any],
    ) -> None:
        """round-26 is untouched by ADR-0040: a super_admin's PERSONAL tag applied
        to existing still reaches every message (not just their own)."""
        acc_admin = await make_mail_account(super_admin.id, "pa@example.com")
        acc_team = await make_secondary_team_mailbox(
            username="pa_owner", group_name="PA-Team", email="pa-team@example.com"
        )
        m_admin = await make_message(acc_admin.id, uid=1, subject="Personal Report a")
        m_team = await make_message(acc_team.id, uid=1, subject="Personal Report b")
        tag_id = await _make_personal_tag(
            db_engine,
            user_id=super_admin.id,
            name="P-Admin",
            rule_type="subject_contains",
            pattern="Report",
        )
        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as ses, ses.begin():
            applied = await TagsService(ses).apply_to_existing(
                user_id=super_admin.id, tag_id=tag_id, is_super_admin=True
            )
        assert applied == 2
        assert tag_id in await _tag_ids_on_message(db_engine, m_admin.id)
        assert tag_id in await _tag_ids_on_message(db_engine, m_team.id)

    async def test_non_super_personal_tag_only_reaches_own(
        self,
        client: Any,
        db_engine: AsyncEngine,
        make_message: Callable[..., Any],
        make_secondary_team_mailbox: Callable[..., Any],
    ) -> None:
        """Silent-regression guard: a non-super_admin's PERSONAL tag applied to
        existing does NOT leak to a foreign team's messages — the global-tag work
        must not have widened personal scope."""
        acc_a = await make_secondary_team_mailbox(
            username="pna_a", group_name="PNA-A", email="pna-a@example.com"
        )
        acc_b = await make_secondary_team_mailbox(
            username="pna_b", group_name="PNA-B", email="pna-b@example.com"
        )
        m_own = await make_message(acc_a.id, uid=1, subject="Mine Report")
        m_foreign = await make_message(acc_b.id, uid=1, subject="Foreign Report")
        # The tag owner is acc_a's owner (a group_member of team A).
        tag_id = await _make_personal_tag(
            db_engine,
            user_id=acc_a.user_id,
            name="P-Member",
            rule_type="subject_contains",
            pattern="Report",
        )
        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as ses, ses.begin():
            applied = await TagsService(ses).apply_to_existing(
                user_id=acc_a.user_id, tag_id=tag_id, is_super_admin=False
            )
        assert applied == 1, "only the owner's own team message is tagged"
        assert tag_id in await _tag_ids_on_message(db_engine, m_own.id)
        assert tag_id not in await _tag_ids_on_message(db_engine, m_foreign.id)
