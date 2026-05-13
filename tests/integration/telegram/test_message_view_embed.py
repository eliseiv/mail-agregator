"""ADR-0022 §2.6: ``GET /messages/{id}?embed=tg`` renders the page as a
Telegram WebApp view (no attachments).

This is a thin templating contract — we verify the rendered HTML omits the
attachment section when ``embed=tg`` is set and the user is authenticated.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from shared.config import get_settings
from shared.crypto import encrypt_mail_password
from shared.models import Attachment, MailAccount, Message, User
from shared.storage import Storage

pytestmark = pytest.mark.integration


async def _login_admin(client: httpx.AsyncClient) -> str:
    s = get_settings()
    await client.post(
        "/login",
        data={"username": s.ADMIN_LOGIN},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    resp = await client.post(
        "/login/password",
        data={"password": s.ADMIN_PASSWORD},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    csrf = resp.cookies.get("mas_csrf")
    assert csrf, resp.text
    return csrf


async def _seed_message_with_attachment(db_engine: AsyncEngine) -> dict[str, Any]:
    s = get_settings()
    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with factory() as ses, ses.begin():
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
            email="embed@example.com",
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
        msg = Message(
            mail_account_id=acc.id,
            uid=200,
            uidvalidity=1,
            from_addr="sender@x.com",
            to_addrs="embed@example.com",
            subject="Embed Subject",
            internal_date=datetime.now(UTC),
            body_text="embed body",
        )
        ses.add(msg)
        await ses.flush()

        # Attach a fake file. The view template renders the "Вложения" block
        # iff message.attachments is non-empty and embed_tg is False.
        from backend.app.repositories.messages import MessagesRepo

        mrepo = MessagesRepo(ses)
        att_id = await mrepo.reserve_attachment_id()
        att_key = Storage.build_key(
            user_id=admin.id,
            mail_account_id=acc.id,
            message_uid=msg.uid,
            attachment_id=att_id,
            filename="attached.txt",
        )
        await mrepo.insert_attachment_with_id(
            attachment_id=att_id,
            message_id=msg.id,
            filename="attached.txt",
            content_type="text/plain",
            size_bytes=11,
            s3_key=att_key,
            skipped_too_large=False,
        )
        return {"msg_id": msg.id, "att_filename": "attached.txt"}


class TestMessageViewEmbedTg:
    async def test_embed_tg_hides_attachments_block(
        self,
        client: httpx.AsyncClient,
        db_engine: AsyncEngine,
    ) -> None:
        await _login_admin(client)
        info = await _seed_message_with_attachment(db_engine)
        resp = await client.get(f"/messages/{info['msg_id']}?embed=tg")
        assert resp.status_code == 200, resp.text
        html = resp.text
        # The attachments <section> must NOT render.
        assert "message__attachments" not in html
        # Sanity: the message subject still renders.
        assert "Embed Subject" in html

    async def test_without_embed_shows_attachments_block(
        self,
        client: httpx.AsyncClient,
        db_engine: AsyncEngine,
    ) -> None:
        await _login_admin(client)
        info = await _seed_message_with_attachment(db_engine)
        resp = await client.get(f"/messages/{info['msg_id']}")
        assert resp.status_code == 200, resp.text
        html = resp.text
        # Attachments block IS rendered.
        assert "message__attachments" in html
        assert info["att_filename"] in html

    async def test_message_view_without_session_redirects_or_401(
        self,
        client: httpx.AsyncClient,
        db_engine: AsyncEngine,
    ) -> None:
        info = await _seed_message_with_attachment(db_engine)
        # No login → middleware sends to /login (302) or returns 401.
        resp = await client.get(f"/messages/{info['msg_id']}?embed=tg")
        assert resp.status_code in (302, 303, 401), resp.text
