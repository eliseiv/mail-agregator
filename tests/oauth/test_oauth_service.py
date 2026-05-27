"""C/D/E. OutlookOAuthService — authorize URL, code exchange, encryption-at-rest.

All token-endpoint interactions are mocked via httpx.MockTransport. Uses the
real Postgres + Redis (rows are committed via ``make_session`` so the service's
own ``_persist_refresh`` session sees them; the autouse truncate fixture cleans
up between tests).
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from cryptography.exceptions import InvalidTag

from backend.app.oauth.schemas import OAUTH_STATE_KEY_PREFIX, OAuthState
from backend.app.oauth.service import OAuthError, OutlookOAuthService
from backend.app.repositories.mail_accounts import MailAccountsRepo
from backend.app.repositories.users import UsersRepo
from shared.crypto import MailPasswordCipher
from shared.db import make_session
from shared.redis_client import get_redis
from tests.oauth._mock_token import TokenEndpoint, token_success_body

pytestmark = pytest.mark.integration


async def _seed_user(username: str = "oauth_owner") -> int:
    async with make_session() as s, s.begin():
        u = await UsersRepo(s).create(username=username, email=None, role="group_member")
        return u.id


# ---------------------------------------------------------------------------
# C. build_authorize_url
# ---------------------------------------------------------------------------


class TestBuildAuthorizeUrl:
    async def test_url_contains_required_params(
        self, enable_outlook_oauth: None, redis_client: object
    ) -> None:
        uid = await _seed_user()
        async with make_session() as s:
            url, state = await OutlookOAuthService(s).build_authorize_url(uid)

        q = parse_qs(urlsplit(url).query)
        assert q["response_type"] == ["code"]
        assert q["client_id"][0]  # mock client id is set
        assert q["code_challenge_method"] == ["S256"]
        assert q["code_challenge"][0]
        assert q["state"] == [state]
        assert "scope" in q

    async def test_state_and_pkce_stored_in_redis_with_ttl(
        self, enable_outlook_oauth: None, redis_client: object
    ) -> None:
        uid = await _seed_user()
        async with make_session() as s:
            _url, state = await OutlookOAuthService(s).build_authorize_url(uid)

        redis = get_redis()
        key = f"{OAUTH_STATE_KEY_PREFIX}{state}"
        raw = await redis.get(key)
        assert raw is not None
        parsed = OAuthState.model_validate_json(raw)
        assert parsed.user_id == uid
        assert parsed.code_verifier  # PKCE verifier persisted
        ttl = await redis.ttl(key)
        assert 0 < ttl <= 600


# ---------------------------------------------------------------------------
# D. exchange_code (callback core)
# ---------------------------------------------------------------------------


async def _store_state(state: str, user_id: int, verifier: str = "verifier-xyz") -> None:
    redis = get_redis()
    payload = OAuthState(user_id=user_id, code_verifier=verifier).model_dump_json()
    await redis.set(f"{OAUTH_STATE_KEY_PREFIX}{state}", payload, ex=600)


class TestExchangeCode:
    async def test_happy_path_creates_oauth_account(
        self, enable_outlook_oauth: None, redis_client: object
    ) -> None:
        uid = await _seed_user()
        await _store_state("st-1", uid)
        ep = TokenEndpoint([httpx.Response(200, json=token_success_body(email="me@outlook.com"))])

        async with make_session() as s, s.begin():
            acc = await OutlookOAuthService(s, http_client=ep.client()).exchange_code(
                code="auth-code", state="st-1"
            )
            acc_id = acc.id

        assert ep.calls == 1
        assert ep.last_request_data["grant_type"] == "authorization_code"

        async with make_session() as s:
            stored = await MailAccountsRepo(s).get_by_id(acc_id)
        assert stored is not None
        assert stored.auth_type == "oauth_outlook"
        assert stored.oauth_provider == "outlook"
        assert stored.email == "me@outlook.com"
        assert stored.encrypted_password is None
        assert stored.oauth_needs_consent is False
        assert stored.imap_host == "outlook.office365.com"

    async def test_refresh_token_encrypted_at_rest(
        self, enable_outlook_oauth: None, redis_client: object
    ) -> None:
        uid = await _seed_user()
        await _store_state("st-2", uid)
        ep = TokenEndpoint(
            [httpx.Response(200, json=token_success_body(refresh_token="RT-secret-123"))]
        )
        async with make_session() as s, s.begin():
            acc = await OutlookOAuthService(s, http_client=ep.client()).exchange_code(
                code="c", state="st-2"
            )
            acc_id = acc.id

        async with make_session() as s:
            stored = await MailAccountsRepo(s).get_by_id(acc_id)
        assert stored is not None
        blob = stored.oauth_refresh_token_encrypted
        assert blob is not None
        assert b"RT-secret-123" not in blob  # ciphertext, not plaintext
        cipher = MailPasswordCipher.from_settings()
        assert cipher.decrypt(blob, acc_id) == "RT-secret-123"

    async def test_state_missing_raises_state_invalid(
        self, enable_outlook_oauth: None, redis_client: object
    ) -> None:
        ep = TokenEndpoint()
        async with make_session() as s:
            with pytest.raises(OAuthError) as exc:
                await OutlookOAuthService(s, http_client=ep.client()).exchange_code(
                    code="c", state="does-not-exist"
                )
        assert exc.value.code == "oauth_state_invalid"
        assert ep.calls == 0  # never reached the token endpoint

    async def test_state_is_one_shot(
        self, enable_outlook_oauth: None, redis_client: object
    ) -> None:
        uid = await _seed_user()
        await _store_state("st-3", uid)
        ep = TokenEndpoint([httpx.Response(200, json=token_success_body(email="x@outlook.com"))])
        async with make_session() as s, s.begin():
            await OutlookOAuthService(s, http_client=ep.client()).exchange_code(
                code="c", state="st-3"
            )
        # Replay the same state -> rejected (GET+DEL consumed it).
        async with make_session() as s:
            with pytest.raises(OAuthError) as exc:
                await OutlookOAuthService(s, http_client=ep.client()).exchange_code(
                    code="c", state="st-3"
                )
        assert exc.value.code == "oauth_state_invalid"

    async def test_token_endpoint_non_200_raises_exchange_failed(
        self, enable_outlook_oauth: None, redis_client: object
    ) -> None:
        uid = await _seed_user()
        await _store_state("st-4", uid)
        ep = TokenEndpoint([httpx.Response(400, json={"error": "invalid_request"})])
        async with make_session() as s:
            with pytest.raises(OAuthError) as exc:
                await OutlookOAuthService(s, http_client=ep.client()).exchange_code(
                    code="c", state="st-4"
                )
        assert exc.value.code == "oauth_exchange_failed"

    async def test_no_refresh_token_raises_exchange_failed(
        self, enable_outlook_oauth: None, redis_client: object
    ) -> None:
        uid = await _seed_user()
        await _store_state("st-5", uid)
        ep = TokenEndpoint([httpx.Response(200, json=token_success_body(refresh_token=None))])
        async with make_session() as s:
            with pytest.raises(OAuthError) as exc:
                await OutlookOAuthService(s, http_client=ep.client()).exchange_code(
                    code="c", state="st-5"
                )
        assert exc.value.code == "oauth_exchange_failed"


# ---------------------------------------------------------------------------
# E. Encryption-at-rest cross-account tamper check.
# ---------------------------------------------------------------------------


class TestEncryptionAtRest:
    async def test_blob_cannot_be_swapped_between_accounts(
        self, enable_outlook_oauth: None, redis_client: object
    ) -> None:
        cipher = MailPasswordCipher.from_settings()
        blob_a = cipher.encrypt("token-A", 1001)
        blob_b = cipher.encrypt("token-B", 1002)
        # Each decrypts under its own id.
        assert cipher.decrypt(blob_a, 1001) == "token-A"
        assert cipher.decrypt(blob_b, 1002) == "token-B"
        # Swapping account_id (AAD) fails with InvalidTag.
        with pytest.raises(InvalidTag):
            cipher.decrypt(blob_a, 1002)
        with pytest.raises(InvalidTag):
            cipher.decrypt(blob_b, 1001)

    async def test_access_and_refresh_tokens_both_decrypt(
        self, enable_outlook_oauth: None, redis_client: object
    ) -> None:
        uid = await _seed_user()
        await _store_state("st-6", uid)
        ep = TokenEndpoint(
            [
                httpx.Response(
                    200,
                    json=token_success_body(access_token="AT-xyz", refresh_token="RT-xyz"),
                )
            ]
        )
        async with make_session() as s, s.begin():
            acc = await OutlookOAuthService(s, http_client=ep.client()).exchange_code(
                code="c", state="st-6"
            )
            acc_id = acc.id
        async with make_session() as s:
            stored = await MailAccountsRepo(s).get_by_id(acc_id)
        assert stored is not None
        cipher = MailPasswordCipher.from_settings()
        assert stored.oauth_access_token_encrypted is not None
        assert stored.oauth_refresh_token_encrypted is not None
        assert cipher.decrypt(stored.oauth_access_token_encrypted, acc_id) == "AT-xyz"
        assert cipher.decrypt(stored.oauth_refresh_token_encrypted, acc_id) == "RT-xyz"
