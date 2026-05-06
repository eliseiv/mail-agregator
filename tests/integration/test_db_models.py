"""Integration tests for the live Postgres schema.

Validates:
- All 7 tables present.
- FK CASCADE: delete user -> mail_accounts/messages/attachments deleted,
  admin_audit ROWS PRESERVED (no FK by design).
- CHECK constraints: ``username = lower(username)``,
  ``NOT (smtp_ssl AND smtp_starttls)``.
- UNIQUE: ``(mail_account_id, uidvalidity, uid)``.

Source of truth: ``docs/03-data-model.md`` + migrations
``20260505_001_initial_schema.py`` + ``20260505_002_lower_username_check.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models import (
    AdminAudit,
    Attachment,
    MailAccount,
    Message,
    User,
)

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Schema presence
# ---------------------------------------------------------------------------


class TestSchema:
    async def test_all_seven_tables_exist(self, db_session: AsyncSession) -> None:
        rows = await db_session.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' ORDER BY table_name"
            )
        )
        names = {r[0] for r in rows.all()}
        expected = {
            "users",
            "mail_accounts",
            "messages",
            "attachments",
            "sent_messages",
            "sent_attachments",
            "admin_audit",
            "alembic_version",
        }
        assert expected.issubset(names), f"missing tables: {expected - names}"


# ---------------------------------------------------------------------------
# CHECK constraints
# ---------------------------------------------------------------------------


class TestCheckConstraints:
    async def test_username_must_be_lower(self, db_session: AsyncSession) -> None:
        # Direct SQL: bypass ORM normalisation to prove the CHECK fires.
        with pytest.raises(IntegrityError):
            async with db_session.begin():
                await db_session.execute(
                    text(
                        "INSERT INTO users (username, is_admin, password_reset_required) "
                        "VALUES ('UpperCase', false, true)"
                    )
                )

    async def test_smtp_ssl_xor_starttls(self, db_session: AsyncSession) -> None:
        # First create a user owner.
        async with db_session.begin():
            user = User(username="alice", is_admin=False, password_reset_required=False)
            db_session.add(user)
            await db_session.flush()
            uid = user.id
        # Now try inserting a mail account with both SSL and STARTTLS.
        with pytest.raises(IntegrityError):
            async with db_session.begin():
                await db_session.execute(
                    text(
                        "INSERT INTO mail_accounts (user_id, email, encrypted_password, "
                        "imap_host, imap_port, imap_ssl, smtp_host, smtp_port, smtp_ssl, "
                        "smtp_starttls) VALUES (:uid, 'a@b.c', '\\x00'::bytea, 'i.b.c', "
                        "993, true, 's.b.c', 465, true, true)"
                    ),
                    {"uid": uid},
                )


# ---------------------------------------------------------------------------
# Unique constraints
# ---------------------------------------------------------------------------


class TestUnique:
    async def test_message_uid_unique_per_account(
        self, db_session: AsyncSession
    ) -> None:
        async with db_session.begin():
            user = User(username="bob", is_admin=False, password_reset_required=False)
            db_session.add(user)
            await db_session.flush()
            account = MailAccount(
                user_id=user.id,
                email="b@b.c",
                encrypted_password=b"\x00",
                imap_host="i.b.c",
                imap_port=993,
                imap_ssl=True,
                smtp_host="s.b.c",
                smtp_port=465,
                smtp_ssl=True,
                smtp_starttls=False,
            )
            db_session.add(account)
            await db_session.flush()
            db_session.add(
                Message(
                    mail_account_id=account.id,
                    uid=100,
                    uidvalidity=1,
                    from_addr="x@y.com",
                    to_addrs="b@b.c",
                    internal_date=datetime.now(UTC),
                )
            )

        with pytest.raises(IntegrityError):
            async with db_session.begin():
                acc = (
                    await db_session.execute(select(MailAccount).limit(1))
                ).scalar_one()
                db_session.add(
                    Message(
                        mail_account_id=acc.id,
                        uid=100,  # duplicate
                        uidvalidity=1,  # same UIDVALIDITY
                        from_addr="x@y.com",
                        to_addrs="b@b.c",
                        internal_date=datetime.now(UTC),
                    )
                )

    async def test_email_unique_per_user(self, db_session: AsyncSession) -> None:
        async with db_session.begin():
            user = User(username="carol", is_admin=False, password_reset_required=False)
            db_session.add(user)
            await db_session.flush()
            db_session.add(
                MailAccount(
                    user_id=user.id,
                    email="dup@b.c",
                    encrypted_password=b"\x00",
                    imap_host="i",
                    imap_port=993,
                    imap_ssl=True,
                    smtp_host="s",
                    smtp_port=465,
                    smtp_ssl=True,
                    smtp_starttls=False,
                )
            )
        with pytest.raises(IntegrityError):
            async with db_session.begin():
                user_row = (
                    await db_session.execute(
                        select(User).where(User.username == "carol")
                    )
                ).scalar_one()
                db_session.add(
                    MailAccount(
                        user_id=user_row.id,
                        email="dup@b.c",  # duplicate
                        encrypted_password=b"\x00",
                        imap_host="i2",
                        imap_port=993,
                        imap_ssl=True,
                        smtp_host="s2",
                        smtp_port=465,
                        smtp_ssl=True,
                        smtp_starttls=False,
                    )
                )


# ---------------------------------------------------------------------------
# FK CASCADE
# ---------------------------------------------------------------------------


class TestCascade:
    async def test_delete_user_cascades_to_accounts_messages_attachments(
        self, db_session: AsyncSession
    ) -> None:
        async with db_session.begin():
            user = User(username="dave", is_admin=False, password_reset_required=False)
            db_session.add(user)
            await db_session.flush()
            account = MailAccount(
                user_id=user.id,
                email="d@b.c",
                encrypted_password=b"\x00",
                imap_host="i",
                imap_port=993,
                imap_ssl=True,
                smtp_host="s",
                smtp_port=465,
                smtp_ssl=True,
                smtp_starttls=False,
            )
            db_session.add(account)
            await db_session.flush()
            msg = Message(
                mail_account_id=account.id,
                uid=1,
                uidvalidity=1,
                from_addr="x@y.com",
                to_addrs="d@b.c",
                internal_date=datetime.now(UTC),
            )
            db_session.add(msg)
            await db_session.flush()
            db_session.add(
                Attachment(
                    message_id=msg.id,
                    filename="a.txt",
                    content_type="text/plain",
                    size_bytes=10,
                    s3_key="dave/1/1/a.txt",
                )
            )
            user_id = user.id

        # Delete the user — everything below should vanish.
        async with db_session.begin():
            await db_session.execute(
                text("DELETE FROM users WHERE id = :u"), {"u": user_id}
            )

        n_accounts = (
            await db_session.execute(
                select(MailAccount).where(MailAccount.user_id == user_id)
            )
        ).scalars().all()
        assert n_accounts == []
        # Counts via direct SQL (more robust than re-issuing ORM through
        # potentially expired objects).
        for table in ("messages", "attachments"):
            n = (
                await db_session.execute(text(f"SELECT count(*) FROM {table}"))
            ).scalar_one()
            assert n == 0, f"{table} not cascaded"

    async def test_delete_user_does_not_remove_admin_audit_rows(
        self, db_session: AsyncSession
    ) -> None:
        async with db_session.begin():
            user = User(username="eve", is_admin=False, password_reset_required=False)
            db_session.add(user)
            await db_session.flush()
            db_session.add(
                AdminAudit(
                    actor_user_id=user.id,
                    action="create_user",
                    target_user_id=user.id,
                    target_username="eve",
                    details={"foo": "bar"},
                    ip="1.2.3.4",
                )
            )
            user_id = user.id

        async with db_session.begin():
            await db_session.execute(
                text("DELETE FROM users WHERE id = :u"), {"u": user_id}
            )

        # admin_audit row preserved.
        rows = (await db_session.execute(select(AdminAudit))).scalars().all()
        assert len(rows) == 1
        assert rows[0].actor_user_id == user_id
        assert rows[0].target_user_id == user_id
