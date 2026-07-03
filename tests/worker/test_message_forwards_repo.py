"""Integration tests for :class:`MessageForwardsRepo` (ADR-0034 §1.2).

The idempotency/claim registry: ``try_reserve`` uses ``INSERT ... ON CONFLICT
(message_id, group_id) DO NOTHING RETURNING id`` so a duplicate claim of the
same ``(message_id, group_id)`` returns ``None`` (exactly-once). ``mark_sent`` /
``mark_error`` finalise the claim in-place.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.repositories.message_forwards import MessageForwardsRepo
from shared.models import MailAccount, MessageForward, User
from shared.models.group import Group
from shared.models.message import Message

pytestmark = pytest.mark.integration  # needs DB

_GID = 4300


async def _seed_message(session: AsyncSession) -> int:
    """Seed group + user + mailbox + one message; return the message id."""
    session.add(Group(id=_GID, name="team-fwd", leader_user_id=None))
    user = User(
        username="mf_repo_user",
        role="super_admin",
        password_hash="$argon2id$v=19$m=65536,t=3,p=4$abc$def",
        password_reset_required=False,
    )
    session.add(user)
    await session.flush()
    acc = MailAccount(
        user_id=user.id,
        group_id=_GID,
        email="box@company.com",
        encrypted_password=b"dummy",
        imap_host="imap.example.com",
        smtp_host="smtp.example.com",
    )
    session.add(acc)
    await session.flush()
    msg = Message(
        mail_account_id=acc.id,
        uid=1,
        uidvalidity=1,
        from_addr="sender@partner.com",
        to_addrs="box@company.com",
        internal_date=datetime.now(UTC),
        body_text="hi",
    )
    session.add(msg)
    await session.flush()
    return msg.id


class TestTryReserve:
    async def test_first_claim_returns_id_duplicate_returns_none(
        self, db_session: AsyncSession
    ) -> None:
        mid = await _seed_message(db_session)
        repo = MessageForwardsRepo(db_session)

        fid = await repo.try_reserve(message_id=mid, group_id=_GID, forward_to="l@c.com")
        assert fid is not None

        # Same (message_id, group_id) → conflict → None (already claimed).
        dup = await repo.try_reserve(message_id=mid, group_id=_GID, forward_to="l@c.com")
        assert dup is None

    async def test_different_group_can_claim_same_message(self, db_session: AsyncSession) -> None:
        mid = await _seed_message(db_session)
        db_session.add(Group(id=_GID + 1, name="team-fwd-2", leader_user_id=None))
        await db_session.flush()
        repo = MessageForwardsRepo(db_session)

        fid_a = await repo.try_reserve(message_id=mid, group_id=_GID, forward_to="a@c.com")
        fid_b = await repo.try_reserve(message_id=mid, group_id=_GID + 1, forward_to="b@c.com")
        assert fid_a is not None and fid_b is not None
        assert fid_a != fid_b


class TestFinalise:
    async def test_mark_sent_sets_sent_at(self, db_session: AsyncSession) -> None:
        mid = await _seed_message(db_session)
        repo = MessageForwardsRepo(db_session)
        fid = await repo.try_reserve(message_id=mid, group_id=_GID, forward_to="l@c.com")
        assert fid is not None
        await repo.mark_sent(fid)
        row = await db_session.get(MessageForward, fid)
        assert row is not None
        assert row.sent_at is not None
        assert row.error is None

    async def test_mark_error_clamps_and_strips_newlines(self, db_session: AsyncSession) -> None:
        mid = await _seed_message(db_session)
        repo = MessageForwardsRepo(db_session)
        fid = await repo.try_reserve(message_id=mid, group_id=_GID, forward_to="l@c.com")
        assert fid is not None
        await repo.mark_error(fid, "boom\r\nsecond line " + "z" * 1000)
        row = await db_session.get(MessageForward, fid)
        assert row is not None
        assert row.sent_at is None
        assert row.error is not None
        assert "\n" not in row.error and "\r" not in row.error
        assert len(row.error) <= 500
