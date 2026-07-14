"""F. OutlookTokenService — cache, refresh, rotation, invalid_grant, lock.

Token endpoint is mocked (httpx.MockTransport). Accounts are committed via
``make_session`` so the service's own ``_persist_refresh`` / ``_await_*``
sessions can read them.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from backend.app.oauth.schemas import OUTLOOK_SCOPES
from backend.app.oauth.service import (
    OAuthRefreshInvalidError,
    OutlookTokenService,
)
from backend.app.repositories.mail_accounts import MailAccountsRepo
from backend.app.repositories.users import UsersRepo
from shared.crypto import MailPasswordCipher
from shared.db import make_session
from shared.models import MailAccount
from tests.oauth._mock_token import TokenEndpoint, token_success_body

pytestmark = pytest.mark.integration


async def _seed_oauth_account(
    *,
    refresh_token: str = "RT-initial",
    access_token: str | None = "AT-cached",
    expires_in_seconds: int | None = 3600,
    needs_consent: bool = False,
) -> int:
    """Insert a committed oauth_outlook account; return its id."""
    async with make_session() as s, s.begin():
        u = await UsersRepo(s).create(username="tok_owner", email=None, role="group_member")
        repo = MailAccountsRepo(s)
        acc_id = await repo.next_account_id()
        cipher = MailPasswordCipher.from_settings()
        refresh_enc = cipher.encrypt(refresh_token, acc_id)
        access_enc = cipher.encrypt(access_token, acc_id) if access_token is not None else None
        expires_at = (
            datetime.now(UTC) + timedelta(seconds=expires_in_seconds)
            if expires_in_seconds is not None
            else None
        )
        await repo.insert_oauth_account_with_id(
            account_id=acc_id,
            user_id=u.id,
            email="box@outlook.com",
            oauth_provider="outlook",
            oauth_refresh_token_encrypted=refresh_enc,
            oauth_access_token_encrypted=access_enc,
            oauth_access_token_expires_at=expires_at,
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


async def _load(acc_id: int) -> MailAccount:
    async with make_session() as s:
        acc = await MailAccountsRepo(s).get_by_id(acc_id)
    assert acc is not None
    return acc


class TestCacheHit:
    async def test_fresh_cached_token_served_without_refresh(self, redis_client: object) -> None:
        acc_id = await _seed_oauth_account(access_token="AT-cached", expires_in_seconds=3600)
        acc = await _load(acc_id)
        ep = TokenEndpoint()
        async with make_session() as s:
            tok = await OutlookTokenService(s, http_client=ep.client()).get_valid_access_token(acc)
        assert tok == "AT-cached"
        assert ep.calls == 0  # cache hit -> no token-endpoint call


class TestRefreshOnExpiry:
    async def test_expired_token_triggers_refresh(self, redis_client: object) -> None:
        acc_id = await _seed_oauth_account(
            access_token="AT-old",
            expires_in_seconds=-10,  # already expired
        )
        acc = await _load(acc_id)
        ep = TokenEndpoint(
            [httpx.Response(200, json=token_success_body(access_token="AT-new", email=None))]
        )
        async with make_session() as s:
            tok = await OutlookTokenService(s, http_client=ep.client()).get_valid_access_token(acc)
        assert tok == "AT-new"
        assert ep.calls == 1
        assert ep.last_request_data["grant_type"] == "refresh_token"

    async def test_rotated_refresh_token_persisted_encrypted(self, redis_client: object) -> None:
        acc_id = await _seed_oauth_account(
            refresh_token="RT-old", access_token=None, expires_in_seconds=None
        )
        acc = await _load(acc_id)
        ep = TokenEndpoint(
            [
                httpx.Response(
                    200,
                    json=token_success_body(
                        access_token="AT-r", refresh_token="RT-rotated", email=None
                    ),
                )
            ]
        )
        async with make_session() as s:
            await OutlookTokenService(s, http_client=ep.client()).get_valid_access_token(acc)
        stored = await _load(acc_id)
        cipher = MailPasswordCipher.from_settings()
        assert stored.oauth_refresh_token_encrypted is not None
        assert cipher.decrypt(stored.oauth_refresh_token_encrypted, acc_id) == "RT-rotated"

    async def test_no_rotation_keeps_old_refresh_token(self, redis_client: object) -> None:
        acc_id = await _seed_oauth_account(
            refresh_token="RT-keep", access_token=None, expires_in_seconds=None
        )
        acc = await _load(acc_id)
        # Microsoft sometimes omits a new refresh token; old one must survive.
        ep = TokenEndpoint(
            [
                httpx.Response(
                    200,
                    json=token_success_body(access_token="AT-r", refresh_token=None, email=None),
                )
            ]
        )
        async with make_session() as s:
            await OutlookTokenService(s, http_client=ep.client()).get_valid_access_token(acc)
        stored = await _load(acc_id)
        cipher = MailPasswordCipher.from_settings()
        assert stored.oauth_refresh_token_encrypted is not None
        assert cipher.decrypt(stored.oauth_refresh_token_encrypted, acc_id) == "RT-keep"


class TestRefreshUsesOutlookScopes:
    """F. Every refresh requests the same single ``OUTLOOK_SCOPES`` set used at
    code-exchange — the direct ``https://outlook.office.com/…`` resource scopes
    (the audience personal-Outlook IMAP/SMTP XOAUTH2 accepts) plus the OIDC +
    offline_access scopes. SINGLE-step config (ADR-0025 §3, P2 reverted). The
    opaque access token is the mock's return value; the issued token's ``aud``
    is determined by Microsoft from this requested ``scope`` — so we assert the
    request scope that pins it."""

    async def test_refresh_requests_outlook_scope_and_updates_access_token(
        self, redis_client: object
    ) -> None:
        acc_id = await _seed_oauth_account(
            access_token="AT-old", refresh_token="RT-x", expires_in_seconds=-10
        )
        acc = await _load(acc_id)
        ep = TokenEndpoint(
            [httpx.Response(200, json=token_success_body(access_token="AT-resource", email=None))]
        )
        async with make_session() as s:
            tok = await OutlookTokenService(s, http_client=ep.client()).get_valid_access_token(acc)

        assert tok == "AT-resource"
        assert ep.calls == 1
        req = ep.requests[0]
        assert req["grant_type"] == "refresh_token"
        assert req["refresh_token"] == "RT-x"
        # The scope is the single OUTLOOK_SCOPES set (direct outlook.office.com
        # resource scopes + OIDC + offline_access), identical to code-exchange.
        scopes = set(req["scope"].split())
        assert scopes == set(OUTLOOK_SCOPES)
        assert "https://outlook.office.com/IMAP.AccessAsUser.All" in scopes

        # The refreshed access token is persisted (cache primed for next call).
        stored = await _load(acc_id)
        cipher = MailPasswordCipher.from_settings()
        assert stored.oauth_access_token_encrypted is not None
        assert cipher.decrypt(stored.oauth_access_token_encrypted, acc_id) == "AT-resource"


class TestInvalidGrant:
    async def test_invalid_grant_marks_needs_consent_and_raises(self, redis_client: object) -> None:
        acc_id = await _seed_oauth_account(access_token=None, expires_in_seconds=None)
        acc = await _load(acc_id)
        ep = TokenEndpoint([httpx.Response(400, json={"error": "invalid_grant"})])
        async with make_session() as s:
            with pytest.raises(OAuthRefreshInvalidError):
                await OutlookTokenService(s, http_client=ep.client()).get_valid_access_token(acc)
        stored = await _load(acc_id)
        assert stored.oauth_needs_consent is True


class TestRefreshLock:
    async def test_concurrent_refresh_calls_token_endpoint_once(self, redis_client: object) -> None:
        """Two parallel get_valid_access_token on the SAME account must do
        exactly one token-endpoint call (Redis SET NX lock — ADR-0025 §3)."""
        acc_id = await _seed_oauth_account(access_token=None, expires_in_seconds=None)

        def _slow_success(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json=token_success_body(access_token="AT-shared", email=None)
            )

        ep = TokenEndpoint([_slow_success])
        # One shared mock client so both coroutines hit the same counter.
        client = ep.client()

        async def _call() -> str:
            async with make_session() as s:
                return await OutlookTokenService(s, http_client=client).get_valid_access_token(
                    await _load(acc_id)
                )

        results = await asyncio.gather(_call(), _call())
        await client.aclose()
        assert results[0] == "AT-shared"
        assert results[1] == "AT-shared"
        assert ep.calls == 1, f"expected exactly one token-endpoint call, got {ep.calls}"

    async def test_redis_down_degrades_to_unlocked_refresh(
        self, redis_client: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If Redis is unavailable, the service must still refresh (best-effort,
        no lock) rather than crash (ADR-0025 §3)."""
        import redis.exceptions as redis_exceptions

        acc_id = await _seed_oauth_account(access_token=None, expires_in_seconds=None)
        acc = await _load(acc_id)
        ep = TokenEndpoint(
            [httpx.Response(200, json=token_success_body(access_token="AT-nolock", email=None))]
        )

        from backend.app.oauth import service as svc_mod

        class _BrokenRedis:
            async def set(self, *a: object, **k: object) -> bool:
                raise redis_exceptions.ConnectionError("redis down")

            def eval(self, *a: object, **k: object) -> int:
                raise redis_exceptions.ConnectionError("redis down")

        monkeypatch.setattr(svc_mod, "get_redis", lambda: _BrokenRedis())

        async with make_session() as s:
            tok = await OutlookTokenService(s, http_client=ep.client()).get_valid_access_token(acc)
        assert tok == "AT-nolock"
        assert ep.calls == 1
