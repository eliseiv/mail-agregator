"""End-to-end test of ADR-0022 §1.3: pending-link redemption on login.

Scenario:

1. ``POST /api/telegram/auth`` for an unknown Telegram user → pending cookie.
2. ``POST /login`` (step-1) + ``POST /login/password`` (step-2) on the same
   client (cookie jar) → on success the login handler reads
   ``mas_tg_pending``, redeems the token, upserts ``telegram_links``, and
   writes an audit row ``telegram_link_created``.
3. A subsequent ``/api/telegram/auth`` with the SAME tg_user_id now returns
   ``linked=true`` (no further login required).
4. Collision: tg_user_X is linked to user_A, then a second pending-redeem
   tries to bind tg_user_Y → user_A: collision audit, link unchanged.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from shared.config import get_settings
from shared.models import AdminAudit, TelegramLink, User
from tests.integration.telegram.conftest import make_init_data

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Helpers (mirror tests/integration/test_auth_flow.py)
# ---------------------------------------------------------------------------


async def _login_two_step(
    client: httpx.AsyncClient, *, username: str, password: str
) -> httpx.Response:
    await client.post(
        "/login",
        data={"username": username},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    return await client.post(
        "/login/password",
        data={"password": password},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )


# ---------------------------------------------------------------------------
# Happy path: pending → redeem → linked
# ---------------------------------------------------------------------------


class TestPendingRedeemOnLogin:
    async def test_login_creates_link_and_audit_after_pending_auth(
        self,
        client: httpx.AsyncClient,
        db_engine: AsyncEngine,
    ) -> None:
        s = get_settings()
        tg_id = 91001

        # Step 1: anonymous SSO call → pending cookie set on the client jar.
        raw = make_init_data(telegram_user_id=tg_id)
        resp = await client.post("/api/telegram/auth", json={"init_data": raw})
        assert resp.status_code == 200
        assert resp.json()["linked"] is False
        # The cookie is stored by httpx jar (we use the same client below).
        assert client.cookies.get("mas_tg_pending") is not None

        # Step 2: complete a normal interactive login.
        login_resp = await _login_two_step(
            client, username=s.ADMIN_LOGIN, password=s.ADMIN_PASSWORD
        )
        assert login_resp.status_code in (302, 303), login_resp.text

        # Step 3: telegram_links row + audit row exist.
        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as ses:
            link = (
                await ses.execute(
                    select(TelegramLink).where(TelegramLink.telegram_user_id == tg_id)
                )
            ).scalar_one_or_none()
            assert link is not None, "telegram_links row not created"
            admin = (
                await ses.execute(select(User).where(User.username == s.ADMIN_LOGIN))
            ).scalar_one()
            assert link.user_id == admin.id
            audits = (
                (
                    await ses.execute(
                        select(AdminAudit).where(AdminAudit.action == "telegram_link_created")
                    )
                )
                .scalars()
                .all()
            )
            assert len(audits) == 1
            details = audits[0].details or {}
            assert details.get("telegram_user_id") == tg_id

    async def test_pending_cookie_is_cleared_after_redeem(
        self,
        client: httpx.AsyncClient,
    ) -> None:
        s = get_settings()
        tg_id = 91002
        raw = make_init_data(telegram_user_id=tg_id)
        await client.post("/api/telegram/auth", json={"init_data": raw})
        assert client.cookies.get("mas_tg_pending") is not None
        resp = await _login_two_step(client, username=s.ADMIN_LOGIN, password=s.ADMIN_PASSWORD)
        # On the login redirect the cookie must be cleared.
        set_cookies = resp.headers.get_list("set-cookie")
        assert any("mas_tg_pending" in c for c in set_cookies)
        # After the redirect httpx's cookie jar applies the clear; check
        # subsequent state.
        # The clear-cookie directive sets value="" + Max-Age=0; the jar
        # ultimately drops it.
        # We accept either deletion or empty-value form.
        cookie_value = client.cookies.get("mas_tg_pending")
        assert cookie_value in (None, "")

    async def test_logout_drops_link_so_subsequent_auth_returns_linked_false(
        self,
        client: httpx.AsyncClient,
    ) -> None:
        """ADR-0022 §1.5: logout DROPS the persistent link. A subsequent
        /api/telegram/auth must therefore return ``linked=false`` and force
        a fresh interactive login.
        """
        s = get_settings()
        tg_id = 91003
        # First call: unlinked → pending cookie set.
        raw = make_init_data(telegram_user_id=tg_id)
        await client.post("/api/telegram/auth", json={"init_data": raw})
        login_resp = await _login_two_step(
            client, username=s.ADMIN_LOGIN, password=s.ADMIN_PASSWORD
        )
        # Grab CSRF so logout passes the middleware check.
        csrf = login_resp.cookies.get("mas_csrf") or client.cookies.get("mas_csrf")
        assert csrf, "CSRF cookie missing after login"
        # Logout — must pass CSRF token.
        logout_resp = await client.post("/logout", headers={"X-CSRF-Token": csrf})
        assert logout_resp.status_code in (302, 303), logout_resp.text

        # Second call with the same tg_id: should be linked=False because
        # the link was revoked at logout.
        raw2 = make_init_data(telegram_user_id=tg_id)
        resp = await client.post("/api/telegram/auth", json={"init_data": raw2})
        assert resp.status_code == 200
        assert resp.json()["linked"] is False

    async def test_link_persists_across_auth_when_session_active(
        self,
        client: httpx.AsyncClient,
    ) -> None:
        """Without an intervening logout, the link survives and a second
        auth returns ``linked=true`` immediately."""
        s = get_settings()
        tg_id = 91004
        raw = make_init_data(telegram_user_id=tg_id)
        await client.post("/api/telegram/auth", json={"init_data": raw})
        await _login_two_step(client, username=s.ADMIN_LOGIN, password=s.ADMIN_PASSWORD)
        # Do NOT logout; just call SSO again with fresh initData.
        raw2 = make_init_data(telegram_user_id=tg_id)
        resp = await client.post("/api/telegram/auth", json={"init_data": raw2})
        assert resp.status_code == 200
        assert resp.json()["linked"] is True


# ---------------------------------------------------------------------------
# Collision (ADR-0022 §1.4)
# ---------------------------------------------------------------------------


class TestCollision:
    async def test_second_tg_for_same_user_writes_collision_audit_and_skips_upsert(
        self,
        client: httpx.AsyncClient,
        db_engine: AsyncEngine,
        make_link: Any,
    ) -> None:
        """If tg_user_X is already linked to user_A and a NEW pending-redeem
        attempts tg_user_Y → user_A, the implementation writes a
        ``telegram_link_collision`` audit row and leaves the existing link
        in place (per :meth:`TelegramSSOService.link_pending`).
        """
        s = get_settings()
        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as ses:
            admin = (
                await ses.execute(select(User).where(User.username == s.ADMIN_LOGIN))
            ).scalar_one()
        admin_id = admin.id
        existing_tg = 92001
        attempted_tg = 92002
        # Pre-existing link.
        await make_link(existing_tg, admin_id)

        # New pending for tg=92002, then login as admin.
        raw = make_init_data(telegram_user_id=attempted_tg)
        await client.post("/api/telegram/auth", json={"init_data": raw})
        login_resp = await _login_two_step(
            client, username=s.ADMIN_LOGIN, password=s.ADMIN_PASSWORD
        )
        assert login_resp.status_code in (302, 303)

        # The original link is untouched.
        async with factory() as ses:
            link = (
                await ses.execute(select(TelegramLink).where(TelegramLink.user_id == admin_id))
            ).scalar_one_or_none()
            assert link is not None
            assert (
                link.telegram_user_id == existing_tg
            ), "existing link must NOT be re-bound to attempted_tg"
            # No telegram_links row was created for attempted_tg.
            attempted_row = (
                await ses.execute(
                    select(TelegramLink).where(TelegramLink.telegram_user_id == attempted_tg)
                )
            ).scalar_one_or_none()
            assert attempted_row is None

            # Collision audit was written.
            collisions = (
                (
                    await ses.execute(
                        select(AdminAudit).where(AdminAudit.action == "telegram_link_collision")
                    )
                )
                .scalars()
                .all()
            )
            assert len(collisions) == 1
            details = collisions[0].details or {}
            assert details.get("existing_telegram_user_id") == existing_tg
            assert details.get("attempted_telegram_user_id") == attempted_tg
