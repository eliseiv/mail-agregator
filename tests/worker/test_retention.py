"""Worker tests for retention_cleanup (post-decommission, ADR-0044 §4 phase A3).

Retention now prunes ``messages`` ONLY — the push-outbox working buffer. The
MinIO attachment cleanup and the ``attachments`` cascade went away with
attachments (ADR-0043 §4), so :class:`CleanupStats` no longer carries
``deleted_attachments_minio`` and the suite no longer seeds a blob.

Source of truth: ``worker/app/cleanup.py`` + ADR-0011 + ADR-0044 §4.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from shared.crypto import encrypt_mail_password
from shared.models import MailAccount, Message, User
from worker.app.cleanup import CleanupStats, retention_cleanup

pytestmark = pytest.mark.integration


@pytest.fixture
async def seeded_old_and_new(db_engine: AsyncEngine) -> dict[str, Any]:
    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with factory() as ses, ses.begin():
        u = User(
            username="cleanup_user",
            role="group_member",
            password_reset_required=False,
        )
        ses.add(u)
        await ses.flush()
        from backend.app.repositories.mail_accounts import MailAccountsRepo

        repo = MailAccountsRepo(ses)
        new_id = await repo.next_account_id()
        blob = encrypt_mail_password("p", new_id)
        a = MailAccount(
            id=new_id,
            user_id=u.id,
            email="cleanup@example.com",
            encrypted_password=blob,
            imap_host="i",
            imap_port=993,
            imap_ssl=True,
            smtp_host="s",
            smtp_port=465,
            smtp_ssl=True,
            smtp_starttls=False,
        )
        ses.add(a)
        await ses.flush()

        # Old message (>30 days) — must be pruned.
        old = Message(
            mail_account_id=a.id,
            uid=1,
            uidvalidity=1,
            from_addr="x@y.com",
            to_addrs="cleanup@example.com",
            internal_date=datetime.now(UTC) - timedelta(days=60),
        )
        ses.add(old)

        # Young message — must survive.
        young = Message(
            mail_account_id=a.id,
            uid=2,
            uidvalidity=1,
            from_addr="x@y.com",
            to_addrs="cleanup@example.com",
            internal_date=datetime.now(UTC) - timedelta(days=1),
        )
        ses.add(young)

    return {"account_id": a.id, "user_id": u.id}


class TestRetention:
    async def test_old_messages_are_deleted_young_survive(
        self,
        seeded_old_and_new: dict[str, Any],
        db_engine: AsyncEngine,
    ) -> None:
        stats = await retention_cleanup()
        assert isinstance(stats, CleanupStats)
        assert stats.deleted_messages >= 1

        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as ses:
            remaining = (await ses.execute(select(Message))).scalars().all()
        assert len(remaining) == 1
        assert remaining[0].uid == 2

    async def test_cleanup_stats_carry_no_minio_counter(self) -> None:
        # ADR-0044 §4 (phase A3): attachments/MinIO are gone — the stats object
        # must not resurrect an attachment counter (a leftover field would mean
        # the MinIO leg is still wired somewhere).
        assert not hasattr(CleanupStats(deleted_messages=0), "deleted_attachments_minio")
