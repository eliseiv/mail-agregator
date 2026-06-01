"""Outlook OAuth2 services (ADR-0025).

``OutlookOAuthService``
    - :meth:`build_authorize_url` — mint state + PKCE (S256), store in Redis,
      assemble the Microsoft authorize URL.
    - :meth:`exchange_code` — validate state (atomic GET+DEL), exchange the
      authorization code for tokens, resolve the mailbox email, create or
      update the ``mail_accounts`` row (encrypted refresh token).

``OutlookTokenService``
    - :meth:`get_valid_access_token` — cache-aware: return the cached access
      token when still fresh, otherwise refresh via the token endpoint
      (handling refresh-token rotation and ``invalid_grant`` -> needs-consent).

Security (docs/06-security.md §1.11 / §2.2):
- ``state`` is a 32-byte URL-safe random, one-shot (GET+DEL), bound to the
  initiating ``user_id``; PKCE ``code_verifier`` stored alongside.
- refresh + access tokens are AES-256-GCM encrypted (MailPasswordCipher,
  AAD=account_id).
- the authorization code, tokens, PKCE verifier and client secret are never
  logged (structlog redact-list + we never pass them as log values).
- httpx uses default TLS verification (``verify=True``) + an explicit timeout.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import secrets
from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import httpx
import redis.exceptions as redis_exceptions
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.oauth.schemas import (
    ACCESS_TOKEN_REFRESH_BUFFER_SECONDS,
    OAUTH_REFRESH_LOCK_PREFIX,
    OAUTH_REFRESH_LOCK_TTL_SECONDS,
    OAUTH_STATE_KEY_PREFIX,
    OUTLOOK_IMAP_HOST,
    OUTLOOK_IMAP_PORT,
    OUTLOOK_SCOPES,
    OUTLOOK_SMTP_HOST,
    OUTLOOK_SMTP_PORT,
    OAuthState,
)
from backend.app.repositories.mail_accounts import MailAccountsRepo
from backend.app.repositories.users import UsersRepo
from shared.config import Settings, get_settings
from shared.crypto import MailPasswordCipher
from shared.db import make_session
from shared.logging import get_logger
from shared.models import MailAccount
from shared.redis_client import get_redis

log = get_logger(__name__)

# Total httpx timeout (connect+read+write) for the Microsoft token endpoint.
_TOKEN_HTTP_TIMEOUT_SECONDS = 15.0

# When another instance holds the per-account refresh lock we briefly wait,
# then re-read the freshly-persisted access token from the DB instead of
# hammering the token endpoint ourselves (ADR-0025 §3 — best-effort, seconds).
_REFRESH_LOCK_WAIT_TOTAL_SECONDS = 3.0
_REFRESH_LOCK_POLL_INTERVAL_SECONDS = 0.25
# DEL-only-if-owner: a tiny Lua script keeps unlock atomic so we never delete a
# lock that has already expired and been re-acquired by another instance.
_RELEASE_LOCK_LUA = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then "
    "return redis.call('del', KEYS[1]) else return 0 end"
)


class OAuthError(Exception):
    """Raised on a recoverable OAuth flow problem; the router maps the
    ``code`` to the documented HTTP error (ADR-0025 §4c)."""

    def __init__(self, code: str, message: str | None = None) -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message or code


class OAuthRefreshInvalidError(Exception):
    """Microsoft returned ``invalid_grant`` — the refresh token is dead and
    the account must be re-consented (ADR-0025 §3 step 5)."""


@dataclass(slots=True)
class _TokenResponse:
    access_token: str
    refresh_token: str | None
    expires_in: int
    scope: str | None
    id_token: str | None


def _b64url_no_pad(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _make_pkce_pair() -> tuple[str, str]:
    """Return ``(code_verifier, code_challenge)`` for PKCE S256 (ADR-0025 §2.3)."""
    verifier = _b64url_no_pad(secrets.token_bytes(32))
    challenge = _b64url_no_pad(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def _decode_email_from_id_token(id_token: str | None) -> str | None:
    """Best-effort extract the mailbox address from an OIDC ``id_token``.

    We do NOT verify the JWT signature here — the token came straight from a
    TLS call to Microsoft's token endpoint in response to our own
    authorization-code exchange, so it is trusted transport-wise (ADR-0025
    §2 step 4). We only need an email-bearing claim: personal Microsoft
    accounts populate it inconsistently, so we probe several known claim names
    (``email`` / ``preferred_username`` / ``upn`` / ``unique_name``).

    On failure we log the *names* of the claims Microsoft sent (never their
    values, and never the raw id_token) to aid debugging without leaking PII or
    secrets.
    """
    if not id_token:
        return None
    parts = id_token.split(".")
    if len(parts) != 3:
        return None
    payload_b64 = parts[1]
    padding = "=" * (-len(payload_b64) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(payload_b64 + padding))
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    for claim in ("email", "preferred_username", "upn", "unique_name"):
        value = payload.get(claim)
        if isinstance(value, str) and "@" in value:
            return value
    # No email-bearing claim found. Log only the claim KEYS (not values) so we
    # can see what Microsoft returned without logging the token or any PII.
    log.warning(
        "oauth.id_token_no_email_claim",
        claim_keys=sorted(payload.keys()),
    )
    return None


class _TokenClient:
    """Thin wrapper over the Microsoft token endpoint.

    Accepts an injected :class:`httpx.AsyncClient` so tests can supply a
    ``MockTransport`` (ADR-0025 Q-OAUTH-3 / TD-031 — no real Azure App needed
    for unit/integration coverage).
    """

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._client = client

    async def _post(self, data: dict[str, str]) -> _TokenResponse:
        endpoint = self._settings.outlook_token_endpoint
        if self._client is not None:
            resp = await self._client.post(endpoint, data=data)
        else:
            async with httpx.AsyncClient(
                timeout=_TOKEN_HTTP_TIMEOUT_SECONDS, verify=True
            ) as client:
                resp = await client.post(endpoint, data=data)

        if resp.status_code != 200:
            body = _safe_token_error(resp)
            if body.get("error") == "invalid_grant":
                raise OAuthRefreshInvalidError(body.get("error_description") or "invalid_grant")
            log.warning(
                "oauth_token_endpoint_error", status=resp.status_code, error=body.get("error")
            )
            raise OAuthError("oauth_exchange_failed", "Token endpoint returned an error")

        payload = resp.json()
        return _TokenResponse(
            access_token=str(payload["access_token"]),
            refresh_token=payload.get("refresh_token"),
            expires_in=int(payload.get("expires_in", 3600)),
            scope=payload.get("scope"),
            id_token=payload.get("id_token"),
        )

    async def exchange_code(self, code: str, code_verifier: str) -> _TokenResponse:
        """Authorization-code -> tokens using the direct resource scopes.

        Single-step flow (ADR-0025 §3, working Sprint-B config): one
        ``code -> token`` request with the EXPLICIT ``https://outlook.office.com/…``
        resource scopes. Yields the access token (used directly for IMAP/SMTP),
        the refresh token (``offline_access``) and the id_token (for email).
        """
        return await self._post(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self._settings.OUTLOOK_REDIRECT_URI,
                "client_id": self._settings.OUTLOOK_CLIENT_ID,
                "client_secret": self._settings.OUTLOOK_CLIENT_SECRET,
                "code_verifier": code_verifier,
                "scope": " ".join(OUTLOOK_SCOPES),
            }
        )

    async def refresh(self, refresh_token: str) -> _TokenResponse:
        """refresh_token grant requesting the same direct ``OUTLOOK_SCOPES``
        (ADR-0025 §3). The issued access_token carries the resource audience
        personal-Outlook IMAP/SMTP XOAUTH2 accepts."""
        return await self._post(
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": self._settings.OUTLOOK_CLIENT_ID,
                "client_secret": self._settings.OUTLOOK_CLIENT_SECRET,
                "scope": " ".join(OUTLOOK_SCOPES),
            }
        )


def _safe_token_error(resp: httpx.Response) -> dict[str, str]:
    """Parse the error body without raising / logging secrets."""
    try:
        data = resp.json()
    except (ValueError, json.JSONDecodeError):
        return {"error": "non_json_error"}
    if not isinstance(data, dict):
        return {"error": "unexpected_body"}
    # Keep only the non-sensitive diagnostic fields.
    return {
        "error": str(data.get("error", "")),
        "error_description": str(data.get("error_description", ""))[:200],
    }


class OutlookOAuthService:
    """Authorize-URL generation + code exchange (ADR-0025 §2)."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        settings: Settings | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._db = session
        self._repo = MailAccountsRepo(session)
        self._users = UsersRepo(session)
        self._settings = settings or get_settings()
        self._cipher = MailPasswordCipher.from_settings(self._settings)
        self._token_client = _TokenClient(self._settings, http_client)

    async def build_authorize_url(self, user_id: int) -> tuple[str, str]:
        """Mint state + PKCE, store in Redis, return ``(authorize_url, state)``."""
        state = secrets.token_urlsafe(32)
        verifier, challenge = _make_pkce_pair()

        redis = get_redis()
        payload = OAuthState(user_id=user_id, code_verifier=verifier).model_dump_json()
        await redis.set(
            f"{OAUTH_STATE_KEY_PREFIX}{state}",
            payload,
            ex=self._settings.OUTLOOK_OAUTH_STATE_TTL_SECONDS,
        )

        params = {
            "client_id": self._settings.OUTLOOK_CLIENT_ID,
            "response_type": "code",
            "redirect_uri": self._settings.OUTLOOK_REDIRECT_URI,
            "scope": " ".join(OUTLOOK_SCOPES),
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "prompt": "select_account",
        }
        authorize_url = f"{self._settings.outlook_authorize_endpoint}?{urlencode(params)}"
        log.info("oauth_authorize_url_built", user_id=user_id)
        return authorize_url, state

    async def _consume_state(self, state: str) -> OAuthState:
        """Atomic GET+DEL of the Redis state; raise on missing/expired."""
        redis = get_redis()
        key = f"{OAUTH_STATE_KEY_PREFIX}{state}"
        async with redis.pipeline(transaction=True) as pipe:
            pipe.get(key)
            pipe.delete(key)
            raw, _deleted = await pipe.execute()
        if not raw:
            raise OAuthError("oauth_state_invalid", "State missing or expired")
        try:
            return OAuthState.model_validate_json(raw)
        except ValueError as exc:
            raise OAuthError("oauth_state_invalid", "State payload corrupt") from exc

    async def exchange_code(self, *, code: str, state: str) -> MailAccount:
        """Validate state, exchange the code, create/update the oauth account.

        Returns the persisted :class:`MailAccount`. Caller (router) writes the
        ``oauth_account_linked`` audit row and commits the transaction.
        """
        st = await self._consume_state(state)

        # Single-step exchange (ADR-0025 §3, working Sprint-B config): one
        # ``code -> token`` request with the direct outlook.office.com resource
        # scopes yields the access token (used directly for IMAP/SMTP), the
        # refresh token and the id_token (for the mailbox email).
        try:
            tokens = await self._token_client.exchange_code(code, st.code_verifier)
        except OAuthRefreshInvalidError as exc:
            # invalid_grant on an authorization-code exchange means the code
            # was already used / expired — treat as a generic exchange failure.
            raise OAuthError("oauth_exchange_failed", "Authorization code rejected") from exc

        if not tokens.refresh_token:
            # offline_access should always yield a refresh token; without one
            # we cannot keep the mailbox synced.
            raise OAuthError("oauth_exchange_failed", "No refresh token returned")

        email = _decode_email_from_id_token(tokens.id_token)
        if not email:
            raise OAuthError("oauth_exchange_failed", "Could not resolve mailbox email")
        email = email.strip().lower()

        owner = await self._users.get_by_id(st.user_id)
        if owner is None:
            raise OAuthError("oauth_state_invalid", "Initiating user no longer exists")

        access_token = tokens.access_token
        refresh_token = tokens.refresh_token
        scopes = tokens.scope
        expires_at = datetime.now(UTC) + timedelta(seconds=tokens.expires_in)

        existing = await self._repo.find_by_user_email(st.user_id, email)
        if existing is not None:
            # Re-consent of an existing account: refresh the stored tokens.
            refresh_enc = self._cipher.encrypt(refresh_token, existing.id)
            access_enc = self._cipher.encrypt(access_token, existing.id)
            await self._repo.update_oauth_tokens(
                existing.id,
                oauth_refresh_token_encrypted=refresh_enc,
                oauth_access_token_encrypted=access_enc,
                oauth_access_token_expires_at=expires_at,
                oauth_scopes=scopes,
                oauth_needs_consent=False,
            )
            refreshed = await self._repo.get_by_id(existing.id)
            assert refreshed is not None
            log.info("oauth_account_relinked", mail_account_id=existing.id, user_id=st.user_id)
            return refreshed

        # New oauth account — predict the id so the AAD binds to it (ADR-0005).
        new_id = await self._repo.next_account_id()
        refresh_enc = self._cipher.encrypt(refresh_token, new_id)
        access_enc = self._cipher.encrypt(access_token, new_id)
        acc = await self._repo.insert_oauth_account_with_id(
            account_id=new_id,
            user_id=st.user_id,
            group_id=owner.group_id,
            email=email,
            oauth_provider="outlook",
            oauth_refresh_token_encrypted=refresh_enc,
            oauth_access_token_encrypted=access_enc,
            oauth_access_token_expires_at=expires_at,
            oauth_scopes=scopes,
            imap_host=OUTLOOK_IMAP_HOST,
            imap_port=OUTLOOK_IMAP_PORT,
            imap_ssl=True,
            smtp_host=OUTLOOK_SMTP_HOST,
            smtp_port=OUTLOOK_SMTP_PORT,
            smtp_ssl=False,
            smtp_starttls=True,
        )
        log.info("oauth_account_linked", mail_account_id=acc.id, user_id=st.user_id)
        return acc


class OutlookTokenService:
    """Cache-aware access-token provider for oauth_outlook accounts (ADR-0025 §3).

    Used by the worker (before IMAP) and the send/test paths (before SMTP).
    Each call opens its own short write transaction when it has to persist a
    refreshed token, so it is safe to invoke outside an existing ``begin()``.
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        settings: Settings | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        # ``session`` is accepted for call-site symmetry with the other
        # services, but token-cache writes use a dedicated ``make_session``
        # (see :meth:`_persist_refresh`) so they never collide with the
        # caller's transaction. No reads go through it — ``account`` is passed
        # in fully-loaded by the caller.
        self._db = session
        self._settings = settings or get_settings()
        self._cipher = MailPasswordCipher.from_settings(self._settings)
        self._token_client = _TokenClient(self._settings, http_client)

    async def get_valid_access_token(self, account: MailAccount) -> str:
        """Return a valid access token, refreshing via the refresh token if needed.

        On Microsoft ``invalid_grant`` the account is flagged
        ``oauth_needs_consent=true`` and :class:`OAuthRefreshInvalidError` is
        re-raised so the caller can skip the account.

        Concurrency (ADR-0025 §3): the worker sync loop and the send/test paths
        can race to refresh the same account; the loser would persist a
        now-rotated (invalid) refresh token and falsely trip
        ``invalid_grant``/needs-consent. We therefore refresh under a
        best-effort Redis lock ``oauth_refresh_lock:{account_id}``. If the lock
        is held elsewhere we briefly wait and re-read the freshly-persisted
        access token; if Redis is unavailable we degrade to an unlocked refresh
        (best-effort — never fatal).
        """
        if account.auth_type != "oauth_outlook":
            raise ValueError("get_valid_access_token requires an oauth_outlook account")

        # 1. Serve the cached access token while it is still comfortably fresh.
        cached = self._cached_access_token(account)
        if cached is not None:
            return cached

        if account.oauth_refresh_token_encrypted is None:
            raise OAuthRefreshInvalidError("missing refresh token")

        # 2. Refresh under a best-effort Redis lock (ADR-0025 §3).
        lock_key = f"{OAUTH_REFRESH_LOCK_PREFIX}{account.id}"
        lock_token = secrets.token_hex(16)
        acquired = await self._try_acquire_lock(lock_key, lock_token)

        if not acquired:
            # Another instance is (probably) refreshing right now. Wait briefly
            # and re-read the token the winner persists, avoiding a duplicate
            # token-endpoint call and the rotation race.
            waited = await self._await_refreshed_token(account.id)
            if waited is not None:
                return waited
            # Still stale after the wait (winner slow / lock was a stale Redis
            # entry). Fall back to an unlocked refresh — best-effort, ADR-0025.
            return await self._do_refresh(account)

        try:
            # Double-check after acquiring: a peer may have refreshed between
            # our cache miss and the lock acquisition, so re-read once and serve
            # the cache if it is now fresh (avoids a redundant token call).
            async with make_session() as s:
                latest = await MailAccountsRepo(s).get_by_id(account.id)
            if latest is not None:
                if latest.oauth_needs_consent:
                    raise OAuthRefreshInvalidError("refresh invalidated by concurrent refresh")
                fresh = self._cached_access_token(latest)
                if fresh is not None:
                    return fresh
                account = latest
            return await self._do_refresh(account)
        finally:
            await self._release_lock(lock_key, lock_token)

    def _cached_access_token(self, account: MailAccount) -> str | None:
        """Return the decrypted cached access token if still comfortably fresh."""
        cached = account.oauth_access_token_encrypted
        expires_at = account.oauth_access_token_expires_at
        if cached is None or expires_at is None:
            return None
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if expires_at > datetime.now(UTC) + timedelta(seconds=ACCESS_TOKEN_REFRESH_BUFFER_SECONDS):
            return self._cipher.decrypt(cached, account.id)
        return None

    async def _do_refresh(self, account: MailAccount) -> str:
        """Perform the token-endpoint refresh + persist the rotated tokens."""
        assert account.oauth_refresh_token_encrypted is not None
        refresh_token = self._cipher.decrypt(account.oauth_refresh_token_encrypted, account.id)

        try:
            tokens = await self._token_client.refresh(refresh_token)
        except OAuthRefreshInvalidError:
            await self._mark_needs_consent(account.id)
            log.warning("oauth_refresh_invalidated", mail_account_id=account.id)
            raise

        new_expires = datetime.now(UTC) + timedelta(seconds=tokens.expires_in)
        access_enc = self._cipher.encrypt(tokens.access_token, account.id)
        # Microsoft may rotate the refresh token — persist the new one if given.
        refresh_enc = (
            self._cipher.encrypt(tokens.refresh_token, account.id) if tokens.refresh_token else None
        )
        await self._persist_refresh(
            account.id,
            access_enc=access_enc,
            refresh_enc=refresh_enc,
            expires_at=new_expires,
            scope=tokens.scope,
        )
        return tokens.access_token

    async def _await_refreshed_token(self, account_id: int) -> str | None:
        """Poll the DB for a freshly-persisted access token while a peer holds
        the refresh lock. Returns the token once fresh, else ``None`` on timeout.
        """
        deadline = asyncio.get_running_loop().time() + _REFRESH_LOCK_WAIT_TOTAL_SECONDS
        while True:
            await asyncio.sleep(_REFRESH_LOCK_POLL_INTERVAL_SECONDS)
            async with make_session() as s:
                acc = await MailAccountsRepo(s).get_by_id(account_id)
            if acc is not None:
                if acc.oauth_needs_consent:
                    # The peer's refresh hit invalid_grant — propagate the same
                    # signal callers already handle.
                    raise OAuthRefreshInvalidError("refresh invalidated by concurrent refresh")
                token = self._cached_access_token(acc)
                if token is not None:
                    return token
            if asyncio.get_running_loop().time() >= deadline:
                return None

    async def _try_acquire_lock(self, key: str, token: str) -> bool:
        """SET NX EX. Returns False if held elsewhere or Redis is unavailable
        (graceful no-op — the caller then refreshes without the lock)."""
        try:
            redis = get_redis()
            ok = await redis.set(key, token, nx=True, ex=OAUTH_REFRESH_LOCK_TTL_SECONDS)
            return bool(ok)
        except (redis_exceptions.RedisError, OSError) as exc:
            log.warning("oauth_refresh_lock_unavailable", error=type(exc).__name__)
            return False

    async def _release_lock(self, key: str, token: str) -> None:
        """Atomically DEL the lock only if we still own it. Best-effort."""
        try:
            redis = get_redis()
            result = redis.eval(_RELEASE_LOCK_LUA, 1, key, token)
            if isinstance(result, Awaitable):
                await result
        except (redis_exceptions.RedisError, OSError) as exc:
            log.warning("oauth_refresh_unlock_failed", error=type(exc).__name__)

    async def _persist_refresh(
        self,
        account_id: int,
        *,
        access_enc: bytes,
        refresh_enc: bytes | None,
        expires_at: datetime,
        scope: str | None,
    ) -> None:
        # Use a dedicated session so this token-cache write is independent of
        # the caller's transaction state — the send path shares the request
        # session (which may already be mid-transaction) and the worker holds
        # a read-only session; a self-contained ``make_session`` avoids the
        # "transaction already begun" trap and commits the rotation promptly.
        async with make_session() as s, s.begin():
            await MailAccountsRepo(s).update_oauth_tokens(
                account_id,
                oauth_refresh_token_encrypted=refresh_enc,
                oauth_access_token_encrypted=access_enc,
                oauth_access_token_expires_at=expires_at,
                oauth_scopes=scope,
                oauth_needs_consent=False,
            )

    async def _mark_needs_consent(self, account_id: int) -> None:
        async with make_session() as s, s.begin():
            await MailAccountsRepo(s).mark_oauth_needs_consent(account_id)
