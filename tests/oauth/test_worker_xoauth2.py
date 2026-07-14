"""H. Worker XOAUTH2 — credential resolution + fetch path + needs-consent skip.

``fetch_blocking`` and ``OutlookTokenService`` are mocked so no IMAP server or
token endpoint is contacted. We assert the worker:
- skips a needs-consent oauth account WITHOUT bumping consecutive_failures;
- resolves an XOAUTH2 access token and passes it (password=None) to fetch;
- leaves password accounts on the classic LOGIN path (access_token=None);
- builds a SASL XOAUTH2 string via the imap_fetcher mailbox.xoauth2 hook.
"""

from __future__ import annotations

import datetime as _dt
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest

from backend.app.repositories.mail_accounts import MailAccountsRepo
from backend.app.repositories.users import UsersRepo
from shared.crypto import MailPasswordCipher
from shared.db import make_session
from shared.models import MailAccount
from worker.app import imap_fetcher as fetcher_mod
from worker.app import sync_cycle as sc

pytestmark = pytest.mark.integration


def _oauth_account(
    *, acc_id: int = 1, needs_consent: bool = False, fresh_access: bool = True
) -> MailAccount:
    cipher = MailPasswordCipher.from_settings()
    return MailAccount(
        id=acc_id,
        user_id=1,
        email="box@outlook.com",
        encrypted_password=None,
        auth_type="oauth_outlook",
        oauth_provider="outlook",
        oauth_refresh_token_encrypted=cipher.encrypt("RT", acc_id),
        oauth_access_token_encrypted=cipher.encrypt("AT-cached", acc_id) if fresh_access else None,
        oauth_access_token_expires_at=datetime.now(UTC) + timedelta(hours=1)
        if fresh_access
        else None,
        oauth_needs_consent=needs_consent,
        imap_host="outlook.office365.com",
        imap_port=993,
        imap_ssl=True,
        smtp_host="smtp-mail.outlook.com",
        smtp_port=587,
        smtp_ssl=False,
        smtp_starttls=True,
    )


def _password_account(acc_id: int = 2) -> MailAccount:
    cipher = MailPasswordCipher.from_settings()
    return MailAccount(
        id=acc_id,
        user_id=1,
        email="pw@example.com",
        encrypted_password=cipher.encrypt("secret", acc_id),
        auth_type="password",
        imap_host="imap.example.com",
        imap_port=993,
        imap_ssl=True,
        smtp_host="smtp.example.com",
        smtp_port=465,
        smtp_ssl=True,
        smtp_starttls=False,
    )


class TestResolveCredentials:
    async def test_needs_consent_skips(self, redis_client: Any) -> None:
        acc = _oauth_account(needs_consent=True)
        log = MagicMock()
        result = await sc._resolve_credentials(acc, log)
        assert result is None  # skip, no fetch

    async def test_oauth_returns_access_token_only(
        self, redis_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        acc = _oauth_account()

        async def _tok(self: Any, account: Any) -> str:
            return "AT-resolved"

        monkeypatch.setattr(
            "backend.app.oauth.service.OutlookTokenService.get_valid_access_token", _tok
        )
        result = await sc._resolve_credentials(acc, MagicMock())
        assert result == (None, "AT-resolved")  # (password, access_token)

    async def test_password_returns_password_only(self, redis_client: Any) -> None:
        acc = _password_account()
        result = await sc._resolve_credentials(acc, MagicMock())
        assert result is not None
        password, access_token = result
        assert password == "secret"
        assert access_token is None


class TestSyncOneAccountOAuth:
    async def _seed_committed_oauth_account(self, needs_consent: bool = False) -> int:
        async with make_session() as s, s.begin():
            u = await UsersRepo(s).create(username="wk_owner", email=None, role="group_member")
            repo = MailAccountsRepo(s)
            acc_id = await repo.next_account_id()
            cipher = MailPasswordCipher.from_settings()
            await repo.insert_oauth_account_with_id(
                account_id=acc_id,
                user_id=u.id,
                email="box@outlook.com",
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

    async def test_oauth_fetch_receives_access_token_not_password(
        self, redis_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        acc_id = await self._seed_committed_oauth_account()
        async with make_session() as s:
            acc = await MailAccountsRepo(s).get_by_id(acc_id)
        assert acc is not None

        captured: dict[str, Any] = {}

        def _fake_fetch(**kwargs: Any) -> fetcher_mod.FetchedBox:
            captured.update(kwargs)
            return fetcher_mod.FetchedBox(uidvalidity=1, uidnext=1, new_messages=[])

        monkeypatch.setattr(sc, "fetch_blocking", _fake_fetch)

        async def _tok(self: Any, account: Any) -> str:
            return "AT-worker"

        monkeypatch.setattr(
            "backend.app.oauth.service.OutlookTokenService.get_valid_access_token", _tok
        )

        # ADR-0026: ``sync_one_account`` returns an ``_AccountResult`` (not a tuple).
        result = await sc.sync_one_account(
            acc,
            timeout_seconds=30,
            initial_sync_days=30,
            max_body_bytes=1_000_000,
            max_att_bytes=1_000_000,
        )
        assert (result.new_count, result.conflict_count) == (0, 0)
        assert captured["access_token"] == "AT-worker"
        assert captured["password"] is None

    async def test_needs_consent_account_skipped_no_failure_bump(
        self, redis_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        acc_id = await self._seed_committed_oauth_account(needs_consent=True)
        async with make_session() as s:
            acc = await MailAccountsRepo(s).get_by_id(acc_id)
        assert acc is not None

        called = {"fetch": 0}

        def _fake_fetch(**_: Any) -> fetcher_mod.FetchedBox:
            called["fetch"] += 1
            return fetcher_mod.FetchedBox(uidvalidity=1, uidnext=1, new_messages=[])

        monkeypatch.setattr(sc, "fetch_blocking", _fake_fetch)

        result = await sc.sync_one_account(
            acc,
            timeout_seconds=30,
            initial_sync_days=30,
            max_body_bytes=1_000_000,
            max_att_bytes=1_000_000,
        )
        assert (result.new_count, result.conflict_count) == (0, 0)
        assert called["fetch"] == 0  # never connected
        async with make_session() as s:
            after = await MailAccountsRepo(s).get_by_id(acc_id)
        assert after is not None
        assert after.consecutive_failures == 0  # no failure bump


class TestFetchBlockingXoauth2:
    """imap_fetcher.fetch_blocking must drive mailbox.xoauth2 when given a token."""

    def _fake_mailbox(self) -> MagicMock:
        mb = MagicMock()
        mb.folder.status.side_effect = lambda folder, items: (
            {"UIDVALIDITY": 1} if "UIDVALIDITY" in items else {"UIDNEXT": 1}
        )
        mb.uids.return_value = []
        return mb

    def test_access_token_drives_xoauth2(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mb = self._fake_mailbox()
        monkeypatch.setattr(fetcher_mod, "_open_mailbox", lambda **_: mb)
        fetcher_mod.fetch_blocking(
            host="outlook.office365.com",
            port=993,
            ssl_on=True,
            username="box@outlook.com",
            access_token="AT-tok",
            last_synced_uidnext=None,
            last_uidvalidity=None,
            initial_sync_days=30,
            max_body_bytes=1000,
            max_att_bytes=1000,
            timeout=30,
        )
        mb.xoauth2.assert_called_once_with("box@outlook.com", "AT-tok", initial_folder="INBOX")
        mb.login.assert_not_called()

    def test_password_drives_login(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mb = self._fake_mailbox()
        monkeypatch.setattr(fetcher_mod, "_open_mailbox", lambda **_: mb)
        fetcher_mod.fetch_blocking(
            host="imap.example.com",
            port=993,
            ssl_on=True,
            username="pw@example.com",
            password="secret",
            last_synced_uidnext=None,
            last_uidvalidity=None,
            initial_sync_days=30,
            max_body_bytes=1000,
            max_att_bytes=1000,
            timeout=30,
        )
        mb.login.assert_called_once()
        mb.xoauth2.assert_not_called()


# Silence unused-import lint for _dt (kept for parity with fetcher test style).
_ = _dt
