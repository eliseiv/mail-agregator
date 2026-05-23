"""Fixtures for the tag-matching / webhook-isolation SQL tests.

These tests exercise the raw SQL in ``backend/app/tags/sql.py`` and
``backend/app/repositories/webhooks.py`` directly against Postgres. They
use the function-scoped, rolled-back ``db_session`` fixture (from the
top-level ``tests/conftest.py``) — only Postgres is required, no redis /
minio / app lifespan. Everything a test needs (users, groups, mail
accounts, messages, tags, rules, links) is seeded inside the session and
discarded on rollback at teardown.

Source of truth:
- ADR-0017 §4.1/§4.2/§5/§5.1/§7 (whole-word, normalised, case-sensitive
  matching + super_admin reach).
- ADR-0023 §3.2/§3.5 (webhook channel isolated from super_admin tags).
- docs/05-modules.md §17/§19.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.tags.sql import APPLY_TAG_TO_EXISTING, APPLY_TAGS_TO_MESSAGE
from shared.models import (
    Group,
    MailAccount,
    Message,
    MessageTag,
    Tag,
    TagRule,
    User,
    Webhook,
)

# ---------------------------------------------------------------------------
# Seed helpers — all operate inside the test's rolled-back db_session.
# ---------------------------------------------------------------------------


class Seeder:
    """Thin builder around ``db_session`` to create domain rows + run the
    two production SQL queries.

    ``flush`` (not ``commit``) is used throughout: the session opened by the
    ``db_session`` fixture rolls back at teardown so nothing persists.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.s = session
        # Monotonic counters so unique constraints never collide within a test.
        self._uname = 0
        self._uid = 0

    # --- users / groups ---------------------------------------------------

    async def super_admin(self, username: str | None = None) -> User:
        self._uname += 1
        u = User(
            username=username or f"sa_{self._uname}",
            role="super_admin",
            group_id=None,
            password_reset_required=False,
        )
        self.s.add(u)
        await self.s.flush()
        return u

    async def group_with_leader(self, name: str) -> tuple[Group, User]:
        self._uname += 1
        leader = User(
            username=f"leader_{self._uname}",
            role="group_leader",
            password_reset_required=False,
        )
        self.s.add(leader)
        await self.s.flush()
        g = Group(name=name, leader_user_id=leader.id)
        self.s.add(g)
        await self.s.flush()
        leader.group_id = g.id
        await self.s.flush()
        return g, leader

    async def member(self, group_id: int) -> User:
        self._uname += 1
        u = User(
            username=f"member_{self._uname}",
            role="group_member",
            group_id=group_id,
            password_reset_required=False,
        )
        self.s.add(u)
        await self.s.flush()
        return u

    # --- mail accounts / messages ----------------------------------------

    async def mail_account(self, *, user_id: int, group_id: int | None, email: str) -> MailAccount:
        # next id from the real sequence so the row is well-formed.
        new_id = int(
            (await self.s.execute(text("SELECT nextval('mail_accounts_id_seq')"))).scalar_one()
        )
        acc = MailAccount(
            id=new_id,
            user_id=user_id,
            group_id=group_id,
            email=email,
            encrypted_password=b"x",
            imap_host="imap.example.com",
            imap_port=993,
            imap_ssl=True,
            smtp_host="smtp.example.com",
            smtp_port=465,
            smtp_ssl=True,
            smtp_starttls=False,
        )
        self.s.add(acc)
        await self.s.flush()
        return acc

    async def message(
        self,
        *,
        mail_account_id: int,
        subject: str | None = "Subject",
        body_text: str = "body",
        from_addr: str = "sender@x.com",
        from_name: str | None = None,
        internal_date: datetime | None = None,
    ) -> Message:
        self._uid += 1
        m = Message(
            mail_account_id=mail_account_id,
            uid=self._uid,
            uidvalidity=1,
            from_addr=from_addr,
            from_name=from_name,
            to_addrs="me@example.com",
            subject=subject,
            internal_date=internal_date or datetime.now(UTC),
            body_text=body_text,
        )
        self.s.add(m)
        await self.s.flush()
        return m

    # --- tags / rules / links --------------------------------------------

    async def tag(
        self,
        *,
        user_id: int,
        name: str,
        match_mode: str = "any",
        rules: list[tuple[str, str]] | None = None,
        color: str = "#aabbcc",
    ) -> Tag:
        t = Tag(user_id=user_id, name=name, color=color, match_mode=match_mode)
        self.s.add(t)
        await self.s.flush()
        for rtype, pattern in rules or []:
            self.s.add(TagRule(tag_id=t.id, type=rtype, pattern=pattern))
        await self.s.flush()
        return t

    async def link(self, *, message_id: int, tag_id: int) -> None:
        self.s.add(MessageTag(message_id=message_id, tag_id=tag_id))
        await self.s.flush()

    async def webhook(self, *, group_id: int, url: str = "https://example.com/hook") -> Webhook:
        new_id = int((await self.s.execute(text("SELECT nextval('webhooks_id_seq')"))).scalar_one())
        w = Webhook(
            id=new_id,
            group_id=group_id,
            url=url,
            secret_encrypted=b"secret-blob",
            is_active=True,
            consecutive_failures=0,
        )
        self.s.add(w)
        await self.s.flush()
        # created_at defaults to now(); ensure messages used in webhook tests
        # are >= it by default (callers can backdate created_at if needed).
        return w

    # --- production SQL runners -------------------------------------------

    async def apply_tags_to_message(self, *, message: Message, mail_account_id: int) -> None:
        """Run APPLY_TAGS_TO_MESSAGE (worker auto-tag path)."""
        await self.s.execute(
            text(APPLY_TAGS_TO_MESSAGE),
            {
                "message_id": message.id,
                "mail_account_id": mail_account_id,
                "subject": message.subject or "",
                "body": message.body_text or "",
                "sender": message.from_addr,
                "sender_name": message.from_name,
            },
        )
        await self.s.flush()

    async def apply_tag_to_existing(
        self, *, tag_id: int, user_id: int, user_group_id: int | None, is_super_admin: bool
    ) -> None:
        """Run APPLY_TAG_TO_EXISTING (apply-to-existing path)."""
        await self.s.execute(
            text(APPLY_TAG_TO_EXISTING),
            {
                "tag_id": tag_id,
                "user_id": user_id,
                "user_group_id": user_group_id,
                "is_super_admin": is_super_admin,
            },
        )
        await self.s.flush()

    async def tags_on_message(self, message_id: int) -> set[int]:
        rows = await self.s.execute(
            text("SELECT tag_id FROM message_tags WHERE message_id = :mid"),
            {"mid": message_id},
        )
        return {int(r[0]) for r in rows}


@pytest_asyncio.fixture
async def seed(db_session: AsyncSession) -> Seeder:
    return Seeder(db_session)


# A convenience type alias for tests that build their own coroutines.
SeedFactory = Callable[[], Awaitable[Seeder]]
