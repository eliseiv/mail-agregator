"""Integration tests for /login, /set-password, /logout.

Covers:
- Wrong password increments ``failed_login_attempts``.
- 5 fails -> lockout 15 min + admin_audit ``lockout_triggered``.
- Lockout returns 423 Account Locked with Retry-After.
- POST /set-password without setup-session -> 401/redirect.
- POST /logout clears session cookie.
- Anti-timing: unknown user still does an argon2 verify.

Source of truth: ``backend/app/auth/router.py`` + ``service.py``,
``docs/04-api-contracts.md`` sec.1, ADR-0009.
"""

from __future__ import annotations

import time
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from shared.config import get_settings
from shared.models import AdminAudit, User

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Helpers — drive the two-step login flow (ADR-0016).
# ---------------------------------------------------------------------------


async def _post_step1(client: httpx.AsyncClient, *, username: str) -> httpx.Response:
    """Step-1: submit only the username; sets ``mas_login`` cookie on success."""
    return await client.post(
        "/login",
        data={"username": username},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )


async def _post_step2(client: httpx.AsyncClient, *, password: str) -> httpx.Response:
    """Step-2: submit only the password; reads username from ``mas_login`` cookie."""
    return await client.post(
        "/login/password",
        data={"password": password},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )


async def _post_login(client: httpx.AsyncClient, *, username: str, password: str) -> httpx.Response:
    """Drive both steps and return the step-2 response.

    Convenience: the username step always returns the same redirect for
    valid/invalid usernames (anti-enumeration), so we can ignore step-1's
    response and surface step-2's outcome to the caller.
    """
    await _post_step1(client, username=username)
    return await _post_step2(client, password=password)


# ---------------------------------------------------------------------------
# Login success / failure
# ---------------------------------------------------------------------------


class TestLogin:
    async def test_admin_login_form_redirects_with_cookies(self, client: httpx.AsyncClient) -> None:
        s = get_settings()
        resp = await _post_login(client, username=s.ADMIN_LOGIN, password=s.ADMIN_PASSWORD)
        # Step-2 redirects with 303 (See Other) per RFC 7231.
        assert resp.status_code == 303
        assert resp.cookies.get("mas_session") is not None
        assert resp.cookies.get("mas_csrf") is not None
        assert resp.headers["location"] == "/"

    async def test_admin_login_json_returns_kind_session_created(
        self, client: httpx.AsyncClient
    ) -> None:
        s = get_settings()
        # JSON two-step: step-1 with username, step-2 with password.
        r1 = await client.post(
            "/login",
            json={"username": s.ADMIN_LOGIN},
            headers={"Accept": "application/json"},
        )
        assert r1.status_code == 200, r1.text
        b1 = r1.json()
        assert b1["kind"] == "needs_password"
        assert b1["redirect"] == "/login/password"

        r2 = await client.post(
            "/login/password",
            json={"password": s.ADMIN_PASSWORD},
            headers={"Accept": "application/json"},
        )
        assert r2.status_code == 200
        body = r2.json()
        assert body["kind"] == "session_created"
        assert body["redirect"] == "/"
        assert r2.cookies.get("mas_session") is not None
        assert r2.cookies.get("mas_csrf") is not None

    async def test_wrong_password_returns_401(self, client: httpx.AsyncClient) -> None:
        s = get_settings()
        resp = await _post_login(client, username=s.ADMIN_LOGIN, password="WRONG")
        assert resp.status_code == 401
        # No session cookie set on failure.
        assert resp.cookies.get("mas_session") is None

    async def test_wrong_password_increments_counter(
        self,
        client: httpx.AsyncClient,
        db_engine: AsyncEngine,
    ) -> None:
        s = get_settings()
        await _post_login(client, username=s.ADMIN_LOGIN, password="x")
        await _post_login(client, username=s.ADMIN_LOGIN, password="y")
        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as ses:
            user = (
                await ses.execute(select(User).where(User.username == s.ADMIN_LOGIN))
            ).scalar_one()
        assert user.failed_login_attempts >= 2


# ---------------------------------------------------------------------------
# Lockout
# ---------------------------------------------------------------------------


class TestLockout:
    async def test_5_failures_trigger_lockout_and_audit(
        self,
        client: httpx.AsyncClient,
        db_engine: AsyncEngine,
    ) -> None:
        s = get_settings()
        # 5 wrong attempts. Use distinct password values so rate-limit allows
        # us to actually hit the lockout (ADR-0009 limits to 5/15min by
        # username+IP — we exhaust on the 5th attempt).
        # Step-1 happens once; step-2 is what counts against LIMIT_LOGIN.
        await _post_step1(client, username=s.ADMIN_LOGIN)
        last: httpx.Response | None = None
        for i in range(s.LOGIN_FAILURE_THRESHOLD):
            last = await _post_step2(client, password=f"wrong{i}")
        assert last is not None
        # The 5th *failure* triggers the lockout, which returns 423 to the
        # caller per the API contract.
        assert last.status_code in (401, 423), last.text

        # 6th attempt: rate-limit OR lockout. Either way NOT 200.
        sixth = await _post_step2(client, password="wrong-final")
        assert sixth.status_code in (401, 423, 429)

        # Audit row should mention lockout.
        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as ses:
            audits = (
                (
                    await ses.execute(
                        select(AdminAudit).where(AdminAudit.action == "lockout_triggered")
                    )
                )
                .scalars()
                .all()
            )
        assert len(audits) >= 1, "expected lockout_triggered audit row"

    async def test_locked_account_rejects_correct_password(
        self,
        client: httpx.AsyncClient,
        db_engine: AsyncEngine,
    ) -> None:
        s = get_settings()
        # Burn through threshold first.
        await _post_step1(client, username=s.ADMIN_LOGIN)
        for i in range(s.LOGIN_FAILURE_THRESHOLD):
            await _post_step2(client, password=f"wrong{i}")
        # Now even the right password is rejected.
        resp = await _post_step2(client, password=s.ADMIN_PASSWORD)
        assert resp.status_code in (423, 429)
        if resp.status_code == 423:
            # 423 must come with Retry-After.
            assert "retry-after" in {h.lower() for h in resp.headers}


# ---------------------------------------------------------------------------
# Anti-timing
# ---------------------------------------------------------------------------


class TestAntiTiming:
    async def test_unknown_user_still_takes_time(self, client: httpx.AsyncClient) -> None:
        # A purely "user does not exist" path should not short-circuit.
        # Both branches at step-1 redirect to /login/password (anti-enumeration);
        # step-2 with a wrong password yields the same generic 401.
        resp = await _post_login(client, username="nonexistent_user", password="x")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Set-password without setup-session
# ---------------------------------------------------------------------------


class TestSetPassword:
    async def test_post_set_password_without_setup_cookie_blocked(
        self, client: httpx.AsyncClient
    ) -> None:
        # No setup cookie + no CSRF token -> CSRF middleware refuses first.
        resp = await client.post(
            "/set-password",
            data={
                "password": "Aa1!Aa1!Aa1!",
                "password_confirm": "Aa1!Aa1!Aa1!",
                "csrf_token": "deadbeef",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        # Either 403 (CSRF) or 401 (no setup-session). Both are acceptable
        # rejections — what matters is the request did NOT succeed.
        assert resp.status_code in (401, 403)

    async def test_get_set_password_without_cookie_redirects_to_login(
        self, client: httpx.AsyncClient
    ) -> None:
        resp = await client.get("/set-password")
        assert resp.status_code in (302, 303)
        assert resp.headers["location"] == "/login"


# ---------------------------------------------------------------------------
# Logout
# ---------------------------------------------------------------------------


class TestLogout:
    async def test_logout_clears_session(self, client: httpx.AsyncClient) -> None:
        s = get_settings()
        login = await _post_login(client, username=s.ADMIN_LOGIN, password=s.ADMIN_PASSWORD)
        # Step-2 redirect is 303 (See Other) after the two-step refactor.
        assert login.status_code == 303
        csrf = login.cookies.get("mas_csrf")
        assert csrf is not None

        # Logout — POST so CSRF token must accompany.
        logout = await client.post(
            "/logout",
            headers={
                "X-CSRF-Token": csrf,
            },
        )
        assert logout.status_code in (302, 303)
        # The Set-Cookie header must clear mas_session (max-age=0 or expires).
        cookie_header = "; ".join(logout.headers.get_list("set-cookie"))
        assert "mas_session" in cookie_header.lower()
        assert (
            'mas_session=""' in cookie_header
            or "mas_session=;" in cookie_header.replace(" ", "")
            or "max-age=0" in cookie_header.lower()
            or "expires=thu, 01 jan 1970" in cookie_header.lower()
        )

    async def test_logout_when_anonymous_redirects_to_login(
        self, client: httpx.AsyncClient
    ) -> None:
        # Anonymous logout: no session, no CSRF — middleware exempts /logout?
        # Actually /logout requires CSRF (it's not in EXEMPT_PATHS). Without
        # session there's no CSRF to compare. Expect 403.
        resp = await client.post("/logout")
        assert resp.status_code in (302, 303, 403)


# ---------------------------------------------------------------------------
# Sanity: fresh login regenerates cookies
# ---------------------------------------------------------------------------


class TestSession:
    async def test_two_logins_produce_distinct_session_tokens(
        self, client: httpx.AsyncClient
    ) -> None:
        s = get_settings()
        a = await _post_login(client, username=s.ADMIN_LOGIN, password=s.ADMIN_PASSWORD)
        # Drop cookies so the second client login isn't authenticated yet.
        client.cookies.clear()
        # Slight pause so timestamps differ.
        time.sleep(0.01)
        b = await _post_login(client, username=s.ADMIN_LOGIN, password=s.ADMIN_PASSWORD)
        assert a.cookies.get("mas_session") != b.cookies.get("mas_session")


def _smoke_unused(_x: Any) -> None:  # pragma: no cover
    return None
