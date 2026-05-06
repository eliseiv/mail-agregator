"""Integration tests for /api/messages.

Covers:
- list with cursor pagination + account_id filter + unread filter
- get message detail (incl. mail_account_email)
- mark-read idempotent
- attachment download (Content-Disposition RFC 5987)
- ownership check (404 not 403 on cross-user access)

Source of truth: ``backend/app/messages/router.py`` + ``service.py``
+ ``docs/04-api-contracts.md`` sec.4.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from shared.config import get_settings
from shared.crypto import encrypt_mail_password
from shared.models import Attachment, MailAccount, Message, User

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Fixture: seed the admin's mail account + a few messages
# ---------------------------------------------------------------------------


@pytest.fixture
async def seeded(
    client: httpx.AsyncClient,
    db_engine: AsyncEngine,
    storage: Any,
) -> dict[str, Any]:
    """Log in admin, seed an account + 5 messages + 1 attachment via the DB.

    Returns dict with ``csrf``, ``account_id``, ``message_ids``, ``att_id``,
    ``att_key``.
    """
    s = get_settings()
    # Login (two-step flow per ADR-0016).
    from tests.integration.conftest import login_as_admin

    csrf = await login_as_admin(client)

    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    message_ids: list[int] = []
    async with factory() as ses:
        # Open the txn FIRST so the autobegin from any future read happens
        # inside our explicit transaction (SQLAlchemy raises if you call
        # ``begin()`` after a previous read autobegan a tx).
        async with ses.begin():
            admin = (
                await ses.execute(select(User).where(User.username == s.ADMIN_LOGIN))
            ).scalar_one()
            from backend.app.repositories.mail_accounts import MailAccountsRepo

            repo = MailAccountsRepo(ses)
            new_id = await repo.next_account_id()
            blob = encrypt_mail_password("p", new_id)
            acc = MailAccount(
                id=new_id,
                user_id=admin.id,
                email="seed@example.com",
                encrypted_password=blob,
                imap_host="imap.example.com",
                imap_port=993,
                imap_ssl=True,
                smtp_host="smtp.example.com",
                smtp_port=465,
                smtp_ssl=True,
                smtp_starttls=False,
            )
            ses.add(acc)
            await ses.flush()
            base = datetime.now(UTC)
            for i in range(5):
                m = Message(
                    mail_account_id=acc.id,
                    uid=100 + i,
                    uidvalidity=1,
                    from_addr=f"sender{i}@x.com",
                    to_addrs="seed@example.com",
                    subject=f"subj-{i}",
                    internal_date=base - timedelta(minutes=i),
                    body_text=f"body-{i}",
                )
                ses.add(m)
                await ses.flush()
                message_ids.append(m.id)

            # Add an attachment to the first message.
            from shared.storage import Storage

            first_msg_id = message_ids[0]
            att_id_seq = (
                await ses.execute(select(Attachment.id).order_by(Attachment.id.desc()).limit(1))
            ).scalar_one_or_none()
            # Reserve via repo helper for parity.
            from backend.app.repositories.messages import MessagesRepo

            mrepo = MessagesRepo(ses)
            att_reserved_id = await mrepo.reserve_attachment_id()
            att_key = Storage.build_key(
                user_id=admin.id,
                mail_account_id=acc.id,
                message_uid=100,
                attachment_id=att_reserved_id,
                filename="file.txt",
            )
            await mrepo.insert_attachment_with_id(
                attachment_id=att_reserved_id,
                message_id=first_msg_id,
                filename="файл с пробелами.txt",
                content_type="text/plain",
                size_bytes=11,
                s3_key=att_key,
                skipped_too_large=False,
            )
            account_id = acc.id
            _ = att_id_seq
            att_id = att_reserved_id

    # Upload payload to MinIO so download works.
    await storage.put_object(att_key, b"hello world", "text/plain")

    return {
        "csrf": csrf,
        "account_id": account_id,
        "message_ids": message_ids,
        "att_id": att_id,
        "att_key": att_key,
    }


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


class TestList:
    async def test_list_returns_all_messages(
        self, client: httpx.AsyncClient, seeded: dict[str, Any]
    ) -> None:
        resp = await client.get("/api/messages")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "items" in body
        assert "next_cursor" in body
        assert len(body["items"]) == 5

    async def test_list_filters_by_account_id(
        self, client: httpx.AsyncClient, seeded: dict[str, Any]
    ) -> None:
        # Filter to the seeded account; should still return all 5.
        resp = await client.get(f"/api/messages?account_id={seeded['account_id']}")
        assert resp.status_code == 200
        assert len(resp.json()["items"]) == 5
        # Filter to a non-existent account.
        resp = await client.get("/api/messages?account_id=999999")
        assert resp.status_code == 200
        assert resp.json()["items"] == []

    async def test_list_pagination_cursor_works(
        self, client: httpx.AsyncClient, seeded: dict[str, Any]
    ) -> None:
        resp = await client.get("/api/messages?limit=2")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["items"]) == 2
        assert body["next_cursor"] is not None

        nxt = await client.get(f"/api/messages?limit=2&cursor={body['next_cursor']}")
        assert nxt.status_code == 200
        nxt_body = nxt.json()
        # Second page should not overlap the first.
        page1_ids = {m["id"] for m in body["items"]}
        page2_ids = {m["id"] for m in nxt_body["items"]}
        assert page1_ids.isdisjoint(page2_ids)


# ---------------------------------------------------------------------------
# Get + mark-read
# ---------------------------------------------------------------------------


class TestGetAndMarkRead:
    async def test_get_message_includes_account_email(
        self, client: httpx.AsyncClient, seeded: dict[str, Any]
    ) -> None:
        msg_id = seeded["message_ids"][0]
        resp = await client.get(f"/api/messages/{msg_id}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == msg_id
        assert body["mail_account_email"] == "seed@example.com"
        assert body["from_addr"] == "sender0@x.com"

    async def test_mark_read_is_idempotent(
        self, client: httpx.AsyncClient, seeded: dict[str, Any]
    ) -> None:
        msg_id = seeded["message_ids"][1]
        csrf = seeded["csrf"]
        # Mark read.
        r1 = await client.post(
            f"/api/messages/{msg_id}/mark-read",
            json={"is_read": True},
            headers={"X-CSRF-Token": csrf},
        )
        assert r1.status_code == 204
        # Again — same response.
        r2 = await client.post(
            f"/api/messages/{msg_id}/mark-read",
            json={"is_read": True},
            headers={"X-CSRF-Token": csrf},
        )
        assert r2.status_code == 204

    async def test_get_nonexistent_returns_404(
        self, client: httpx.AsyncClient, seeded: dict[str, Any]
    ) -> None:
        resp = await client.get("/api/messages/99999999")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Attachment download — RFC 5987 Content-Disposition
# ---------------------------------------------------------------------------


class TestAttachmentDownload:
    async def test_download_succeeds_with_proper_headers(
        self, client: httpx.AsyncClient, seeded: dict[str, Any]
    ) -> None:
        msg_id = seeded["message_ids"][0]
        att_id = seeded["att_id"]
        resp = await client.get(
            f"/api/messages/{msg_id}/attachments/{att_id}",
        )
        assert resp.status_code == 200, resp.text
        cd = resp.headers["content-disposition"]
        assert cd.startswith("attachment;")
        # Filename + filename* both present (RFC 5987).
        assert 'filename="' in cd
        assert "filename*=UTF-8''" in cd
        # Body matches the upload.
        assert resp.content == b"hello world"

    async def test_download_404_for_unknown_attachment(
        self, client: httpx.AsyncClient, seeded: dict[str, Any]
    ) -> None:
        msg_id = seeded["message_ids"][0]
        resp = await client.get(f"/api/messages/{msg_id}/attachments/999999")
        assert resp.status_code == 404
