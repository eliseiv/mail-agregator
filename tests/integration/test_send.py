"""Integration tests for /api/messages/send.

SMTP and IMAP-append are mocked. Verifies:
- JSON path returns SendMessageResponse.
- Form path 303-redirects with flash.
- Multi-value to/cc/bcc parsed correctly.
- sent_messages row inserted.

Source of truth: ``backend/app/send/router.py`` + ``service.py``,
``docs/04-api-contracts.md`` sec.5.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from shared.config import get_settings
from shared.crypto import encrypt_mail_password
from shared.models import MailAccount, SentMessage, User

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _mock_smtp_and_imap(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Mock aiosmtplib.send + the IMAP append helper."""
    captured: dict[str, Any] = {"recipients": None, "ok": True}

    import aiosmtplib

    async def _fake_send(*_args: Any, **kwargs: Any) -> Any:
        captured["recipients"] = kwargs.get("recipients")
        return None, "OK"

    monkeypatch.setattr(aiosmtplib, "send", _fake_send)

    # Mock imap append helper used inside SendService.
    from backend.app.send import service as svc_mod

    def _fake_blocking(**_: Any) -> None:
        return None

    monkeypatch.setattr(svc_mod, "_imap_append_blocking", _fake_blocking)
    return captured


@pytest.fixture
async def admin_account(
    client: httpx.AsyncClient, db_engine: AsyncEngine
) -> dict[str, Any]:
    s = get_settings()
    resp = await client.post(
        "/login",
        data={"username": s.ADMIN_LOGIN, "password": s.ADMIN_PASSWORD},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert resp.status_code == 302
    csrf = resp.cookies["mas_csrf"]

    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with factory() as ses, ses.begin():
        admin = (
            await ses.execute(select(User).where(User.username == s.ADMIN_LOGIN))
        ).scalar_one()
        from backend.app.repositories.mail_accounts import MailAccountsRepo

        repo = MailAccountsRepo(ses)
        new_id = await repo.next_account_id()
        blob = encrypt_mail_password("p", new_id)
        ses.add(
            MailAccount(
                id=new_id,
                user_id=admin.id,
                email="from@example.com",
                encrypted_password=blob,
                imap_host="imap.example.com",
                imap_port=993,
                imap_ssl=True,
                smtp_host="smtp.example.com",
                smtp_port=465,
                smtp_ssl=True,
                smtp_starttls=False,
            )
        )
        account_id = new_id
    return {"csrf": csrf, "account_id": account_id, "admin_id": admin.id}


class TestSend:
    async def test_send_json_returns_response(
        self,
        client: httpx.AsyncClient,
        admin_account: dict[str, Any],
        db_engine: AsyncEngine,
    ) -> None:
        resp = await client.post(
            "/api/messages/send",
            json={
                "from_account_id": admin_account["account_id"],
                "to": ["a@x.com"],
                "subject": "hello",
                "body": "hi",
            },
            headers={"X-CSRF-Token": admin_account["csrf"]},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "sent_id" in body
        assert "smtp_message_id" in body
        assert body["smtp_message_id"].startswith("<") and body["smtp_message_id"].endswith(">")
        # Row persisted.
        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as ses:
            rows = (await ses.execute(select(SentMessage))).scalars().all()
        assert len(rows) == 1
        assert rows[0].subject == "hello"
        assert rows[0].to_addrs == "a@x.com"

    async def test_send_form_redirects(
        self,
        client: httpx.AsyncClient,
        admin_account: dict[str, Any],
    ) -> None:
        resp = await client.post(
            "/api/messages/send",
            data={
                "csrf_token": admin_account["csrf"],
                "from_account_id": str(admin_account["account_id"]),
                "to": "a@x.com,b@x.com",
                "cc": "c@x.com;d@x.com",
                "subject": "hi",
                "body": "y",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/"

    async def test_multi_value_recipients_passed_to_smtp(
        self,
        client: httpx.AsyncClient,
        admin_account: dict[str, Any],
        _mock_smtp_and_imap: dict[str, Any],
    ) -> None:
        await client.post(
            "/api/messages/send",
            data={
                "csrf_token": admin_account["csrf"],
                "from_account_id": str(admin_account["account_id"]),
                "to": "a@x.com, b@x.com",
                "cc": "c@x.com",
                "bcc": "secret@x.com",
                "subject": "y",
                "body": "z",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        # The SMTP send received all 4 envelope addresses.
        recipients = _mock_smtp_and_imap["recipients"]
        assert recipients is not None
        assert set(recipients) == {"a@x.com", "b@x.com", "c@x.com", "secret@x.com"}

    async def test_send_to_unknown_account_returns_404(
        self,
        client: httpx.AsyncClient,
        admin_account: dict[str, Any],
    ) -> None:
        resp = await client.post(
            "/api/messages/send",
            json={
                "from_account_id": 999999,
                "to": ["a@x.com"],
                "subject": "x",
                "body": "x",
            },
            headers={"X-CSRF-Token": admin_account["csrf"]},
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "not_found"
