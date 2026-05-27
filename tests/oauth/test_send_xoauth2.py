"""I. Send XOAUTH2 — needs-consent 409 + SMTP XOAUTH2 branch (mocked).

Uses the live app via ``oauth_client`` (sending from an existing oauth account
does not depend on the feature flag). The SMTP XOAUTH2 send and IMAP append +
token refresh are mocked.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from backend.app.repositories.mail_accounts import MailAccountsRepo
from backend.app.repositories.users import UsersRepo
from shared.config import get_settings
from shared.crypto import MailPasswordCipher
from shared.db import make_session
from tests.oauth.conftest import two_step_login

pytestmark = pytest.mark.integration


async def _login_admin(client: httpx.AsyncClient) -> str:
    s = get_settings()
    resp = await two_step_login(client, s.ADMIN_LOGIN, s.ADMIN_PASSWORD)
    assert resp.status_code in (302, 303), resp.text
    csrf = resp.cookies.get("mas_csrf")
    assert csrf
    return csrf


async def _seed_oauth_account(*, needs_consent: bool = False) -> int:
    async with make_session() as s, s.begin():
        admin = await UsersRepo(s).get_admin()
        assert admin is not None
        repo = MailAccountsRepo(s)
        acc_id = await repo.next_account_id()
        cipher = MailPasswordCipher.from_settings()
        await repo.insert_oauth_account_with_id(
            account_id=acc_id,
            user_id=admin.id,
            group_id=None,
            email="from@outlook.com",
            oauth_provider="outlook",
            oauth_refresh_token_encrypted=cipher.encrypt("RT", acc_id),
            oauth_access_token_encrypted=cipher.encrypt("AT-cached", acc_id),
            oauth_access_token_expires_at=datetime.now(UTC) + timedelta(hours=1),
            oauth_scopes="scope",
            imap_host="outlook.office365.com",
            imap_port=993,
            imap_ssl=True,
            smtp_host="smtp-mail.outlook.com",
            smtp_port=587,
            smtp_ssl=False,
            smtp_starttls=True,
        )
        if needs_consent:
            await repo.mark_oauth_needs_consent(acc_id)
    return acc_id


@pytest.fixture
def _mock_send_paths(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    from backend.app.send import service as svc_mod

    calls: dict[str, Any] = {"smtp_oauth": 0, "imap_oauth": 0, "token": 0, "args": None}

    async def _fake_smtp_oauth(**kwargs: Any) -> None:
        calls["smtp_oauth"] += 1
        calls["args"] = kwargs

    def _fake_imap_oauth(**_: Any) -> None:
        calls["imap_oauth"] += 1

    async def _fake_token(self: Any, account: Any) -> str:
        calls["token"] += 1
        return "AT-send"

    monkeypatch.setattr(svc_mod, "_smtp_send_oauth", _fake_smtp_oauth)
    monkeypatch.setattr(svc_mod, "_imap_append_oauth_blocking", _fake_imap_oauth)
    monkeypatch.setattr(
        "backend.app.oauth.service.OutlookTokenService.get_valid_access_token", _fake_token
    )
    return calls


class TestSendOAuth:
    async def test_send_uses_xoauth2_branch(
        self, oauth_client: httpx.AsyncClient, _mock_send_paths: dict[str, Any]
    ) -> None:
        client = oauth_client
        csrf = await _login_admin(client)
        acc_id = await _seed_oauth_account()
        resp = await client.post(
            "/api/messages/send",
            json={
                "from_account_id": acc_id,
                "to": ["dest@example.com"],
                "subject": "hi",
                "body": "body",
            },
            headers={"X-CSRF-Token": csrf},
        )
        assert resp.status_code == 200, resp.text
        assert _mock_send_paths["token"] == 1
        assert _mock_send_paths["smtp_oauth"] == 1  # XOAUTH2 send, not LOGIN
        # Token threaded through to the SMTP XOAUTH2 helper.
        assert _mock_send_paths["args"]["access_token"] == "AT-send"
        assert _mock_send_paths["args"]["email"] == "from@outlook.com"

    async def test_needs_consent_returns_409_without_send(
        self, oauth_client: httpx.AsyncClient, _mock_send_paths: dict[str, Any]
    ) -> None:
        client = oauth_client
        csrf = await _login_admin(client)
        acc_id = await _seed_oauth_account(needs_consent=True)
        resp = await client.post(
            "/api/messages/send",
            json={
                "from_account_id": acc_id,
                "to": ["dest@example.com"],
                "subject": "hi",
                "body": "body",
            },
            headers={"X-CSRF-Token": csrf},
        )
        assert resp.status_code == 409, resp.text
        assert resp.json()["error"]["code"] == "oauth_reconsent_required"
        # Rejected before any token refresh / SMTP connect.
        assert _mock_send_paths["token"] == 0
        assert _mock_send_paths["smtp_oauth"] == 0
