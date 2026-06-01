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

from backend.app.oauth.schemas import (
    OAUTH_STATE_KEY_PREFIX,
    OUTLOOK_AUTHORIZE_SCOPES,
    OUTLOOK_RESOURCE_SCOPES,
    OAuthState,
)
from backend.app.oauth.service import OAuthError, OutlookOAuthService
from backend.app.repositories.mail_accounts import MailAccountsRepo
from backend.app.repositories.users import UsersRepo
from shared.crypto import MailPasswordCipher
from shared.db import make_session
from shared.redis_client import get_redis
from tests.oauth._mock_token import TokenEndpoint, token_success_body, two_step_responses

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
        ep = TokenEndpoint(two_step_responses(email="me@outlook.com"))

        async with make_session() as s, s.begin():
            acc = await OutlookOAuthService(s, http_client=ep.client()).exchange_code(
                code="auth-code", state="st-1"
            )
            acc_id = acc.id

        # P2 two-step: code-exchange (step1) + immediate resource refresh (step2).
        assert ep.calls == 2
        assert ep.requests[0]["grant_type"] == "authorization_code"
        assert ep.requests[1]["grant_type"] == "refresh_token"

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
        # Step 2 does NOT rotate (refresh_token=None) -> step 1's refresh token
        # must survive and be the one persisted (spec C).
        ep = TokenEndpoint(
            two_step_responses(
                step1_refresh_token="RT-secret-123",
                step2_refresh_token=None,
            )
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
        ep = TokenEndpoint(two_step_responses(email="x@outlook.com"))
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
        # Persisted access token is the RESOURCE token from step 2 (not step 1's
        # short-scope one); persisted refresh is step 2's rotated token.
        ep = TokenEndpoint(
            two_step_responses(
                step1_access_token="AT-step1-WRONG-aud",
                step2_access_token="AT-xyz",
                step2_refresh_token="RT-xyz",
            )
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


# ---------------------------------------------------------------------------
# P2 two-step audience-correction flow (ADR-0025 §3/§5). exchange_code must:
#   step 1 — authorization_code grant with SHORT-form scopes (id_token+refresh)
#   step 2 — immediate refresh_token grant with RESOURCE scopes (the persisted
#            access token, correctly-audienced for personal-Outlook IMAP).
# ---------------------------------------------------------------------------


def _scope_set(form: dict[str, str]) -> set[str]:
    """Split a captured request's ``scope`` form field into a set."""
    return set(form.get("scope", "").split())


class TestTwoStepExchangeP2:
    # --- A. exactly two token calls with the correct per-step scope ----------
    async def test_exchange_makes_exactly_two_calls_with_correct_scopes(
        self, enable_outlook_oauth: None, redis_client: object
    ) -> None:
        uid = await _seed_user()
        await _store_state("p2-A", uid)
        ep = TokenEndpoint(two_step_responses(email="a@outlook.com"))
        async with make_session() as s, s.begin():
            await OutlookOAuthService(s, http_client=ep.client()).exchange_code(
                code="auth-code", state="p2-A"
            )

        assert ep.calls == 2, f"expected exactly 2 token calls, got {ep.calls}"

        step1, step2 = ep.requests
        # Step 1: authorization_code with SHORT-form scopes.
        assert step1["grant_type"] == "authorization_code"
        assert step1["code"] == "auth-code"
        assert _scope_set(step1) == set(OUTLOOK_AUTHORIZE_SCOPES)
        # Step 2: refresh_token with EXPLICIT outlook.office.com RESOURCE scopes.
        assert step2["grant_type"] == "refresh_token"
        assert _scope_set(step2) == set(OUTLOOK_RESOURCE_SCOPES)
        # Step 2 hands back the step-1 refresh token to upgrade the audience.
        assert step2["refresh_token"] == "RT-step1"

    # --- B. persisted access token is step 2's (resource), not step 1's ------
    async def test_persisted_access_token_is_step2_resource_token(
        self, enable_outlook_oauth: None, redis_client: object
    ) -> None:
        uid = await _seed_user()
        await _store_state("p2-B", uid)
        ep = TokenEndpoint(
            two_step_responses(
                step1_access_token="AT-step1-short-WRONG-aud",
                step2_access_token="AT-step2-resource-RIGHT-aud",
            )
        )
        async with make_session() as s, s.begin():
            acc = await OutlookOAuthService(s, http_client=ep.client()).exchange_code(
                code="c", state="p2-B"
            )
            acc_id = acc.id

        async with make_session() as s:
            stored = await MailAccountsRepo(s).get_by_id(acc_id)
        assert stored is not None and stored.oauth_access_token_encrypted is not None
        cipher = MailPasswordCipher.from_settings()
        decrypted = cipher.decrypt(stored.oauth_access_token_encrypted, acc_id)
        assert decrypted == "AT-step2-resource-RIGHT-aud"
        assert decrypted != "AT-step1-short-WRONG-aud"

    # --- C. refresh token: rotated step-2 if present, else step-1 ------------
    async def test_persisted_refresh_token_prefers_step2_rotation(
        self, enable_outlook_oauth: None, redis_client: object
    ) -> None:
        uid = await _seed_user()
        await _store_state("p2-C1", uid)
        ep = TokenEndpoint(
            two_step_responses(step1_refresh_token="RT-1", step2_refresh_token="RT-2-rotated")
        )
        async with make_session() as s, s.begin():
            acc = await OutlookOAuthService(s, http_client=ep.client()).exchange_code(
                code="c", state="p2-C1"
            )
            acc_id = acc.id
        async with make_session() as s:
            stored = await MailAccountsRepo(s).get_by_id(acc_id)
        assert stored is not None and stored.oauth_refresh_token_encrypted is not None
        cipher = MailPasswordCipher.from_settings()
        assert cipher.decrypt(stored.oauth_refresh_token_encrypted, acc_id) == "RT-2-rotated"

    async def test_persisted_refresh_token_falls_back_to_step1_when_no_rotation(
        self, enable_outlook_oauth: None, redis_client: object
    ) -> None:
        uid = await _seed_user()
        await _store_state("p2-C2", uid)
        ep = TokenEndpoint(
            two_step_responses(step1_refresh_token="RT-1-keep", step2_refresh_token=None)
        )
        async with make_session() as s, s.begin():
            acc = await OutlookOAuthService(s, http_client=ep.client()).exchange_code(
                code="c", state="p2-C2"
            )
            acc_id = acc.id
        async with make_session() as s:
            stored = await MailAccountsRepo(s).get_by_id(acc_id)
        assert stored is not None and stored.oauth_refresh_token_encrypted is not None
        cipher = MailPasswordCipher.from_settings()
        assert cipher.decrypt(stored.oauth_refresh_token_encrypted, acc_id) == "RT-1-keep"

    # --- D. email comes from step-1's id_token (step 2 has none) -------------
    async def test_email_resolved_from_step1_id_token(
        self, enable_outlook_oauth: None, redis_client: object
    ) -> None:
        uid = await _seed_user()
        await _store_state("p2-D", uid)
        ep = TokenEndpoint(two_step_responses(email="Mailbox.Owner@Outlook.com"))
        async with make_session() as s, s.begin():
            acc = await OutlookOAuthService(s, http_client=ep.client()).exchange_code(
                code="c", state="p2-D"
            )
            acc_id = acc.id
        async with make_session() as s:
            stored = await MailAccountsRepo(s).get_by_id(acc_id)
        assert stored is not None
        # Normalised to lower-case (service strips + lower()s the address).
        assert stored.email == "mailbox.owner@outlook.com"

    async def test_step1_without_email_claim_raises_exchange_failed(
        self, enable_outlook_oauth: None, redis_client: object
    ) -> None:
        # Step 1 id_token carries no email-bearing claim -> cannot resolve the
        # mailbox -> fail BEFORE the step-2 resource refresh is ever attempted.
        uid = await _seed_user()
        await _store_state("p2-D2", uid)
        ep = TokenEndpoint(two_step_responses(email=None))
        async with make_session() as s:
            with pytest.raises(OAuthError) as exc:
                await OutlookOAuthService(s, http_client=ep.client()).exchange_code(
                    code="c", state="p2-D2"
                )
        assert exc.value.code == "oauth_exchange_failed"
        assert ep.calls == 1  # step 2 never reached

    # --- E. authorize_url carries SHORT-form scopes --------------------------
    async def test_authorize_url_uses_short_form_scopes(
        self, enable_outlook_oauth: None, redis_client: object
    ) -> None:
        uid = await _seed_user()
        async with make_session() as s:
            url, _state = await OutlookOAuthService(s).build_authorize_url(uid)
        q = parse_qs(urlsplit(url).query)
        scopes = set(q["scope"][0].split())
        assert scopes == set(OUTLOOK_AUTHORIZE_SCOPES)
        # SHORT form: no https:// resource prefix on the delegated scopes.
        assert "IMAP.AccessAsUser.All" in scopes
        assert "https://outlook.office.com/IMAP.AccessAsUser.All" not in scopes
        # OIDC + offline_access present so the id_token + refresh are issued.
        assert {"offline_access", "openid", "email", "profile"} <= scopes

    # --- G. invalid_scope guard: resource scopes carry NO OIDC scopes --------
    async def test_resource_scopes_exclude_oidc_scopes(self) -> None:
        # Mixing reserved OIDC scopes (openid/email/profile) with explicit
        # resource scopes in a refresh grant triggers Microsoft 'invalid_scope'.
        resource = set(OUTLOOK_RESOURCE_SCOPES)
        assert "openid" not in resource
        assert "email" not in resource
        assert "profile" not in resource
        # But offline_access IS kept so the refresh token survives rotation.
        assert "offline_access" in resource
        assert "https://outlook.office.com/IMAP.AccessAsUser.All" in resource
        assert "https://outlook.office.com/SMTP.Send" in resource

    # --- H (exchange side). invalid_grant on step 2 -> oauth_exchange_failed --
    async def test_invalid_grant_on_step2_upgrade_raises_exchange_failed(
        self, enable_outlook_oauth: None, redis_client: object
    ) -> None:
        uid = await _seed_user()
        await _store_state("p2-H", uid)
        # Step 1 succeeds, step 2 (resource upgrade) returns invalid_grant.
        step1, _step2_ok = two_step_responses(email="h@outlook.com")
        ep = TokenEndpoint([step1, httpx.Response(400, json={"error": "invalid_grant"})])
        async with make_session() as s:
            with pytest.raises(OAuthError) as exc:
                await OutlookOAuthService(s, http_client=ep.client()).exchange_code(
                    code="c", state="p2-H"
                )
        # invalid_grant on the upgrade-refresh is surfaced as a generic exchange
        # failure (NOT needs-consent — there is no account yet to flag).
        assert exc.value.code == "oauth_exchange_failed"
        assert ep.calls == 2  # both steps were attempted
