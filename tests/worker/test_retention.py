"""Worker tests for retention_cleanup.

Source of truth: ``worker/app/cleanup.py`` + ADR-0011 +
``docs/05-modules.md`` sec.15.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from shared.crypto import encrypt_mail_password
from shared.models import Attachment, MailAccount, Message, User
from worker.app.cleanup import retention_cleanup

pytestmark = pytest.mark.integration


@pytest.fixture
async def seeded_old_and_new(db_engine: AsyncEngine, storage: Any) -> dict[str, Any]:
    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    keys: list[str] = []
    async with factory() as ses, ses.begin():
        u = User(
            username="cleanup_user",
            is_admin=False,
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

        # Old message (>30 days) + attachment.
        old = Message(
            mail_account_id=a.id,
            uid=1,
            uidvalidity=1,
            from_addr="x@y.com",
            to_addrs="cleanup@example.com",
            internal_date=datetime.now(UTC) - timedelta(days=60),
        )
        ses.add(old)
        await ses.flush()
        from backend.app.repositories.messages import MessagesRepo
        from shared.storage import Storage

        mrepo = MessagesRepo(ses)
        att_id = await mrepo.reserve_attachment_id()
        att_key = Storage.build_key(
            user_id=u.id,
            mail_account_id=a.id,
            message_uid=1,
            attachment_id=att_id,
            filename="old.txt",
        )
        await mrepo.insert_attachment_with_id(
            attachment_id=att_id,
            message_id=old.id,
            filename="old.txt",
            content_type="text/plain",
            size_bytes=3,
            s3_key=att_key,
            skipped_too_large=False,
        )
        keys.append(att_key)

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

    # Upload the old attachment to MinIO so cleanup can verify deletion.
    await storage.put_object(keys[0], b"old", "text/plain")
    return {"keys": keys}


class TestRetention:
    async def test_old_messages_and_blobs_are_deleted(
        self,
        seeded_old_and_new: dict[str, Any],
        db_engine: AsyncEngine,
    ) -> None:
        stats = await retention_cleanup()
        assert stats.deleted_messages >= 1
        assert stats.deleted_attachments_minio >= 1

        # Verify the young message survived.
        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as ses:
            remaining = (await ses.execute(select(Message))).scalars().all()
        assert len(remaining) == 1
        assert remaining[0].uid == 2

        # Old attachment cascade-deleted via the message FK.
        async with factory() as ses:
            atts = (await ses.execute(select(Attachment))).scalars().all()
        assert atts == []
