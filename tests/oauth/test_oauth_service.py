"""C/D/E. OutlookOAuthService — authorize URL, code exchange, encryption-at-rest.

All token-endpoint interactions are mocked via httpx.MockTransport. Uses the
real Postgres + Redis (rows are committed via ``make_session`` so the service's
own ``_persist_refresh`` session sees them; the autouse truncate fixture cleans
up between tests).

SINGLE-STEP flow: the code exchange makes exactly ONE ``code -> token`` request with
the direct ``https://outlook.office.com/…`` resource scopes; the authorize URL carries
the same ``OUTLOOK_SCOPES``. The persisted access token is the one returned by that
single request; the email comes from its ``id_token``.

ADR-0044 §5 / ADR-0045: the SESSION pair (``build_authorize_url(user_id)`` /
``exchange_code(code, state)``) went away with the cookie UI. The surviving flow is the
HEADLESS one (``build_authorize_url_headless(crm_state)`` /
``exchange_code_headless(code, state)``) — same PKCE/state machinery, same token
exchange, same encryption-at-rest, but the owner of a linked mailbox is the technical
``crm-service`` user (ADR-0045 §1) instead of a logged-in human. Every case below is
retargeted onto it; the state payload therefore carries ``crm_state``, never ``user_id``.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from cryptography.exceptions import InvalidTag

from backend.app.auth.service import seed_crm_service_user
from backend.app.oauth.schemas import (
    OAUTH_STATE_KEY_PREFIX,
    OUTLOOK_SCOPES,
    OAuthState,
)
from backend.app.oauth.service import OAuthError, OutlookOAuthService
from backend.app.repositories.mail_accounts import MailAccountsRepo
from shared.crypto import MailPasswordCipher
from shared.db import make_session
from shared.redis_client import get_redis
from tests.oauth._mock_token import TokenEndpoint, token_success_body

pytestmark = pytest.mark.integration


_CRM_STATE = "crm-opaque-state"


async def _seed_owner() -> None:
    """Seed the ``crm-service`` technical user — the owner of any headless-linked box.

    The autouse truncate wipes it between tests, so each case re-seeds (the app
    lifespan does the same on boot, ``seed_crm_service_user`` is idempotent).
    """
    async with make_session() as s, s.begin():
        await seed_crm_service_user(s)


def _scope_set(form: dict[str, str]) -> set[str]:
    """Split a captured request's ``scope`` form field into a set."""
    return set(form.get("scope", "").split())


# ---------------------------------------------------------------------------
# C. build_authorize_url
# ---------------------------------------------------------------------------


class TestBuildAuthorizeUrl:
    async def test_url_contains_required_params(
        self, enable_outlook_oauth: None, redis_client: object
    ) -> None:
        await _seed_owner()
        async with make_session() as s:
            url, state = await OutlookOAuthService(s).build_authorize_url_headless(_CRM_STATE)

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
        await _seed_owner()
        async with make_session() as s:
            _url, state = await OutlookOAuthService(s).build_authorize_url_headless(_CRM_STATE)

        redis = get_redis()
        key = f"{OAUTH_STATE_KEY_PREFIX}{state}"
        raw = await redis.get(key)
        assert raw is not None
        parsed = OAuthState.model_validate_json(raw)
        # Headless payload: the opaque CRM state is stored verbatim and there is NO
        # user_id — the flow has no logged-in human (ADR-0045 §1).
        assert parsed.crm_state == _CRM_STATE
        assert parsed.user_id is None
        assert parsed.code_verifier  # PKCE verifier persisted
        ttl = await redis.ttl(key)
        assert 0 < ttl <= 600

    async def test_authorize_url_uses_outlook_scopes(
        self, enable_outlook_oauth: None, redis_client: object
    ) -> None:
        await _seed_owner()
        async with make_session() as s:
            url, _state = await OutlookOAuthService(s).build_authorize_url_headless(_CRM_STATE)
        q = parse_qs(urlsplit(url).query)
        scopes = set(q["scope"][0].split())
        assert scopes == set(OUTLOOK_SCOPES)
        # Direct resource form: the explicit https://outlook.office.com prefix.
        assert "https://outlook.office.com/IMAP.AccessAsUser.All" in scopes
        assert "https://outlook.office.com/SMTP.Send" in scopes
        # OIDC + offline_access present so the id_token + refresh are issued.
        assert {"offline_access", "openid", "email", "profile"} <= scopes


# ---------------------------------------------------------------------------
# D. exchange_code_headless (callback core) — SINGLE-step.
# ---------------------------------------------------------------------------


async def _store_state(state: str, verifier: str = "verifier-xyz") -> None:
    """Put a HEADLESS state payload (``crm_state`` set, ``user_id`` None) in Redis."""
    redis = get_redis()
    payload = OAuthState(crm_state=_CRM_STATE, code_verifier=verifier).model_dump_json()
    await redis.set(f"{OAUTH_STATE_KEY_PREFIX}{state}", payload, ex=600)


class TestExchangeCodeHeadless:
    async def test_happy_path_creates_oauth_account(
        self, enable_outlook_oauth: None, redis_client: object
    ) -> None:
        await _seed_owner()
        await _store_state("st-1")
        ep = TokenEndpoint([httpx.Response(200, json=token_success_body(email="me@outlook.com"))])

        async with make_session() as s, s.begin():
            acc, _crm = await OutlookOAuthService(
                s, http_client=ep.client()
            ).exchange_code_headless(code="auth-code", state="st-1")
            acc_id = acc.id

        # Single-step: exactly one authorization_code token call.
        assert ep.calls == 1
        assert ep.requests[0]["grant_type"] == "authorization_code"
        assert ep.requests[0]["code"] == "auth-code"
        assert _scope_set(ep.requests[0]) == set(OUTLOOK_SCOPES)

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
        await _seed_owner()
        await _store_state("st-2")
        ep = TokenEndpoint(
            [httpx.Response(200, json=token_success_body(refresh_token="RT-secret-123"))]
        )
        async with make_session() as s, s.begin():
            acc, _crm = await OutlookOAuthService(
                s, http_client=ep.client()
            ).exchange_code_headless(code="c", state="st-2")
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
                await OutlookOAuthService(s, http_client=ep.client()).exchange_code_headless(
                    code="c", state="does-not-exist"
                )
        assert exc.value.code == "oauth_state_invalid"
        assert ep.calls == 0  # never reached the token endpoint

    async def test_state_is_one_shot(
        self, enable_outlook_oauth: None, redis_client: object
    ) -> None:
        await _seed_owner()
        await _store_state("st-3")
        ep = TokenEndpoint([httpx.Response(200, json=token_success_body(email="x@outlook.com"))])
        async with make_session() as s, s.begin():
            await OutlookOAuthService(s, http_client=ep.client()).exchange_code_headless(
                code="c", state="st-3"
            )
        # Replay the same state -> rejected (GET+DEL consumed it).
        async with make_session() as s:
            with pytest.raises(OAuthError) as exc:
                await OutlookOAuthService(s, http_client=ep.client()).exchange_code_headless(
                    code="c", state="st-3"
                )
        assert exc.value.code == "oauth_state_invalid"

    async def test_token_endpoint_non_200_raises_exchange_failed(
        self, enable_outlook_oauth: None, redis_client: object
    ) -> None:
        await _seed_owner()
        await _store_state("st-4")
        ep = TokenEndpoint([httpx.Response(400, json={"error": "invalid_request"})])
        async with make_session() as s:
            with pytest.raises(OAuthError) as exc:
                await OutlookOAuthService(s, http_client=ep.client()).exchange_code_headless(
                    code="c", state="st-4"
                )
        assert exc.value.code == "oauth_exchange_failed"

    async def test_invalid_grant_on_exchange_raises_exchange_failed(
        self, enable_outlook_oauth: None, redis_client: object
    ) -> None:
        # invalid_grant on the authorization_code exchange means the code was
        # already used / expired — surfaced as a generic exchange failure (NOT
        # needs-consent: there is no account yet to flag). ADR-0025 §3.
        await _seed_owner()
        await _store_state("st-ig")
        ep = TokenEndpoint([httpx.Response(400, json={"error": "invalid_grant"})])
        async with make_session() as s:
            with pytest.raises(OAuthError) as exc:
                await OutlookOAuthService(s, http_client=ep.client()).exchange_code_headless(
                    code="c", state="st-ig"
                )
        assert exc.value.code == "oauth_exchange_failed"
        assert ep.calls == 1

    async def test_no_refresh_token_raises_exchange_failed(
        self, enable_outlook_oauth: None, redis_client: object
    ) -> None:
        await _seed_owner()
        await _store_state("st-5")
        ep = TokenEndpoint([httpx.Response(200, json=token_success_body(refresh_token=None))])
        async with make_session() as s:
            with pytest.raises(OAuthError) as exc:
                await OutlookOAuthService(s, http_client=ep.client()).exchange_code_headless(
                    code="c", state="st-5"
                )
        assert exc.value.code == "oauth_exchange_failed"

    async def test_no_email_claim_raises_exchange_failed(
        self, enable_outlook_oauth: None, redis_client: object
    ) -> None:
        # The single exchange response carries no email-bearing id_token claim
        # -> cannot resolve the mailbox -> oauth_exchange_failed.
        await _seed_owner()
        await _store_state("st-noemail")
        ep = TokenEndpoint([httpx.Response(200, json=token_success_body(email=None))])
        async with make_session() as s:
            with pytest.raises(OAuthError) as exc:
                await OutlookOAuthService(s, http_client=ep.client()).exchange_code_headless(
                    code="c", state="st-noemail"
                )
        assert exc.value.code == "oauth_exchange_failed"
        assert ep.calls == 1

    async def test_email_resolved_and_normalised_from_id_token(
        self, enable_outlook_oauth: None, redis_client: object
    ) -> None:
        await _seed_owner()
        await _store_state("st-email")
        ep = TokenEndpoint(
            [httpx.Response(200, json=token_success_body(email="Mailbox.Owner@Outlook.com"))]
        )
        async with make_session() as s, s.begin():
            acc, _crm = await OutlookOAuthService(
                s, http_client=ep.client()
            ).exchange_code_headless(code="c", state="st-email")
            acc_id = acc.id
        async with make_session() as s:
            stored = await MailAccountsRepo(s).get_by_id(acc_id)
        assert stored is not None
        # Service strips + lower()s the address.
        assert stored.email == "mailbox.owner@outlook.com"


# ---------------------------------------------------------------------------
# E. Encryption-at-rest cross-account tamper check + single-exchange tokens.
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
        await _seed_owner()
        await _store_state("st-6")
        # Single-step: the persisted access + refresh tokens are exactly the
        # ones returned by the one ``code -> token`` request.
        ep = TokenEndpoint(
            [
                httpx.Response(
                    200,
                    json=token_success_body(access_token="AT-xyz", refresh_token="RT-xyz"),
                )
            ]
        )
        async with make_session() as s, s.begin():
            acc, _crm = await OutlookOAuthService(
                s, http_client=ep.client()
            ).exchange_code_headless(code="c", state="st-6")
            acc_id = acc.id
        async with make_session() as s:
            stored = await MailAccountsRepo(s).get_by_id(acc_id)
        assert stored is not None
        cipher = MailPasswordCipher.from_settings()
        assert stored.oauth_access_token_encrypted is not None
        assert stored.oauth_refresh_token_encrypted is not None
        assert cipher.decrypt(stored.oauth_access_token_encrypted, acc_id) == "AT-xyz"
        assert cipher.decrypt(stored.oauth_refresh_token_encrypted, acc_id) == "RT-xyz"
