"""End-to-end test of ADR-0022 §1.3: pending-link redemption on login.

Scenario:

1. ``POST /api/telegram/auth`` for an unknown Telegram user → pending cookie.
2. ``POST /login`` (step-1) + ``POST /login/password`` (step-2) on the same
   client (cookie jar) → on success the login handler reads
   ``mas_tg_pending``, redeems the token, upserts ``telegram_links``, and
   writes an audit row ``telegram_link_created``.
3. A subsequent ``/api/telegram/auth`` with the SAME tg_user_id now returns
   ``linked=true`` (no further login required).
4. Multi-TG (ADR-0024 §3): tg_user_X is linked to user_A, then a second
   pending-redeem binds tg_user_Y → user_A. Both links now coexist — the
   old ``telegram_link_collision`` behaviour is removed.
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
        """Without an intervening logout, the link survives. round-38
        (ADR-0022 §1.6): because the second ``/api/telegram/auth`` now carries
        a valid ``mas_session`` (set by the login above), the endpoint enters
        **self-heal** mode instead of the legacy ``linked=true`` flow — it
        idempotently confirms the live binding and answers
        ``{linked:false, healed:true}`` (NO redirect, NO new session). The
        link still persists (covered explicitly by the self-heal NO-OP test in
        ``test_self_heal.py``)."""
        s = get_settings()
        tg_id = 91004
        raw = make_init_data(telegram_user_id=tg_id)
        await client.post("/api/telegram/auth", json={"init_data": raw})
        await _login_two_step(client, username=s.ADMIN_LOGIN, password=s.ADMIN_PASSWORD)
        # Do NOT logout; just call SSO again with fresh initData. The active
        # session routes this into the self-heal branch (round-38).
        raw2 = make_init_data(telegram_user_id=tg_id)
        resp = await client.post("/api/telegram/auth", json={"init_data": raw2})
        assert resp.status_code == 200
        assert resp.json() == {"linked": False, "healed": True}


# ---------------------------------------------------------------------------
# Second TG via login-flow — ADR-0024 §3 (collision logic REMOVED)
# ---------------------------------------------------------------------------
# Pre-ADR-0024 this scenario produced a ``telegram_link_collision`` audit and
# silently dropped the second TG (one user — one TG). ADR-0024 §3 lifts that
# invariant: a second TG bound to the SAME user via the login-flow is now a
# normal ``telegram_link_created`` and BOTH links coexist (multi-TG).


class TestSecondTgViaLoginFlow:
    async def test_second_tg_for_same_user_is_linked_not_a_collision(
        self,
        client: httpx.AsyncClient,
        db_engine: AsyncEngine,
        make_link: Any,
    ) -> None:
        """ADR-0024 §3: tg_user_X is already linked to user_A; a NEW
        pending-redeem binds tg_user_Y → user_A. Expect BOTH links to exist,
        a ``telegram_link_created`` audit for the new one, and NO
        ``telegram_link_collision`` (deprecated, never written)."""
        s = get_settings()
        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as ses:
            admin = (
                await ses.execute(select(User).where(User.username == s.ADMIN_LOGIN))
            ).scalar_one()
        admin_id = admin.id
        existing_tg = 92001
        new_tg = 92002
        # Pre-existing link (created out-of-band).
        await make_link(existing_tg, admin_id)

        # New pending for tg=92002, then login as admin → redeem binds it.
        raw = make_init_data(telegram_user_id=new_tg)
        await client.post("/api/telegram/auth", json={"init_data": raw})
        login_resp = await _login_two_step(
            client, username=s.ADMIN_LOGIN, password=s.ADMIN_PASSWORD
        )
        assert login_resp.status_code in (302, 303)

        async with factory() as ses:
            # BOTH links now belong to admin (multi-TG).
            links = (
                (await ses.execute(select(TelegramLink).where(TelegramLink.user_id == admin_id)))
                .scalars()
                .all()
            )
            tg_ids = {link.telegram_user_id for link in links}
            assert tg_ids == {existing_tg, new_tg}, f"both links expected, got {tg_ids}"

            # A telegram_link_created audit exists for the new TG.
            created = (
                (
                    await ses.execute(
                        select(AdminAudit).where(AdminAudit.action == "telegram_link_created")
                    )
                )
                .scalars()
                .all()
            )
            assert any((a.details or {}).get("telegram_user_id") == new_tg for a in created)

            # The deprecated collision audit is NEVER written under ADR-0024.
            collisions = (
                (
                    await ses.execute(
                        select(AdminAudit).where(AdminAudit.action == "telegram_link_collision")
                    )
                )
                .scalars()
                .all()
            )
            assert collisions == [], "telegram_link_collision is deprecated (ADR-0024 §3)"
