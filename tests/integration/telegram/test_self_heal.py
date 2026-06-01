"""Integration tests for the round-38 self-heal TG binding
(ADR-0022 §1.6 edge-3 — ``docs/04-api-contracts.md`` §4a).

When ``POST /api/telegram/auth`` lands with a **valid ``mas_session``** the
endpoint switches into *self-heal* mode: it idempotently rebinds the proven
``telegram_user_id`` to the already-logged-in user, WITHOUT issuing a second
session or a ``mas_tg_pending`` cookie, and answers
``{"linked": false, "healed": <bool>}`` (no ``redirect``).

The decision table (ADR-0022 §1.6, lines 321-326) is exercised end-to-end:

- missing link            → INSERT, ``created_at=now()``, audit
  ``telegram_link_created replaced=false via=self_heal`` (case 1);
- live link, same user    → **full NO-OP**: ``created_at`` preserved, NO audit
  (case 2 — the critical regression guard against lost messages);
- dead link, same user    → reactivate, ``dead_at=NULL``, ``created_at=now()``,
  audit ``replaced=true via=self_heal`` (case 3);
- link of another user    → rebound onto the current user, audit
  ``telegram_link_rebound via=self_heal`` (case 4).

Plus router-level invariants (case 5), uniform NO-OP via the
``POST /api/telegram/links`` session-add path (case 6) and the best-effort
failure path (case 7).

Source of truth: ``backend/app/telegram/router.py`` +
``backend/app/telegram/sso_service.py`` + ADR-0022 §1.6.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from shared.config import get_settings
from shared.models import AdminAudit, TelegramLink, User
from tests.integration.telegram.conftest import make_init_data

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _login_admin(client: httpx.AsyncClient) -> str:
    """Two-step login as the seeded super-admin; sets ``mas_session`` /
    ``mas_csrf`` on the shared client cookie jar. Returns the CSRF token."""
    s = get_settings()
    await client.post(
        "/login",
        data={"username": s.ADMIN_LOGIN},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    resp = await client.post(
        "/login/password",
        data={"password": s.ADMIN_PASSWORD},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert resp.status_code in (302, 303), resp.text
    csrf = resp.cookies.get("mas_csrf") or client.cookies.get("mas_csrf")
    assert csrf, "csrf cookie missing after login"
    return csrf


async def _admin_id(db_engine: AsyncEngine) -> int:
    s = get_settings()
    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with factory() as ses:
        admin = (await ses.execute(select(User).where(User.username == s.ADMIN_LOGIN))).scalar_one()
        return admin.id


async def _get_link(db_engine: AsyncEngine, tg_id: int) -> TelegramLink | None:
    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with factory() as ses:
        return (
            await ses.execute(select(TelegramLink).where(TelegramLink.telegram_user_id == tg_id))
        ).scalar_one_or_none()


async def _audits(db_engine: AsyncEngine, action: str) -> list[AdminAudit]:
    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with factory() as ses:
        return list(
            (await ses.execute(select(AdminAudit).where(AdminAudit.action == action)))
            .scalars()
            .all()
        )


async def _audit_count(db_engine: AsyncEngine) -> int:
    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with factory() as ses:
        return int((await ses.execute(select(func.count()).select_from(AdminAudit))).scalar_one())


# ---------------------------------------------------------------------------
# Case 1 — self-heal with NO existing link → INSERT, healed=true
# ---------------------------------------------------------------------------


class TestSelfHealInsert:
    async def test_no_link_creates_row_no_cookies_audit_via_self_heal(
        self,
        client: httpx.AsyncClient,
        db_engine: AsyncEngine,
    ) -> None:
        """Valid session + valid initData + NO link → 200 {linked:false,
        healed:true}; row created (created_at set); NO Set-Cookie for
        mas_session/mas_csrf/mas_tg_pending; audit ``telegram_link_created
        via=self_heal replaced=false``."""
        await _login_admin(client)
        admin_id = await _admin_id(db_engine)
        tg_id = 510001

        raw = make_init_data(telegram_user_id=tg_id)
        resp = await client.post("/api/telegram/auth", json={"init_data": raw})

        assert resp.status_code == 200, resp.text
        # Self-heal body: linked=false, healed=true, NO redirect (exclude_none).
        assert resp.json() == {"linked": False, "healed": True}

        # No session / pending cookies issued on the self-heal path.
        set_cookies = resp.headers.get_list("set-cookie")
        for name in ("mas_session", "mas_csrf", "mas_tg_pending"):
            assert not any(
                c.startswith(f"{name}=") for c in set_cookies
            ), f"{name} must not be (re)set on self-heal; got {set_cookies}"

        # Row created and owned by the admin, with created_at set & live.
        link = await _get_link(db_engine, tg_id)
        assert link is not None, "telegram_links row not created by self-heal"
        assert link.user_id == admin_id
        assert link.dead_at is None
        assert link.created_at is not None

        created = await _audits(db_engine, "telegram_link_created")
        assert len(created) == 1
        details = created[0].details or {}
        assert details.get("via") == "self_heal"
        assert details.get("replaced") is False
        assert details.get("telegram_user_id") == tg_id


# ---------------------------------------------------------------------------
# Case 2 — CRITICAL: live link, same user → full NO-OP (created_at frozen,
# no second audit). Guards the recipient SQL window (m.internal_date >=
# tl.created_at): bumping created_at would silently drop messages.
# ---------------------------------------------------------------------------


class TestSelfHealLiveNoOp:
    async def test_two_self_heals_of_live_link_do_not_move_created_at(
        self,
        client: httpx.AsyncClient,
        db_engine: AsyncEngine,
        make_link: Any,
    ) -> None:
        """Two sequential self-heals of a LIVE link of the same user:
        ``created_at`` must NOT change between the calls, and the second call
        must write NO audit (full NO-OP). This is the critical guard against
        lost messages (recipient SQL ``m.internal_date >= tl.created_at``)."""
        csrf = await _login_admin(client)  # noqa: F841 — session cookie is what we need
        admin_id = await _admin_id(db_engine)
        tg_id = 520001

        # Pre-existing LIVE link, created out-of-band (created_at = its insert).
        await make_link(tg_id, admin_id)
        before = await _get_link(db_engine, tg_id)
        assert before is not None and before.dead_at is None
        created_at_0 = before.created_at

        # First self-heal of a live link → NO-OP.
        raw1 = make_init_data(telegram_user_id=tg_id)
        r1 = await client.post("/api/telegram/auth", json={"init_data": raw1})
        assert r1.status_code == 200, r1.text
        assert r1.json() == {"linked": False, "healed": True}

        mid = await _get_link(db_engine, tg_id)
        assert mid is not None
        created_at_1 = mid.created_at
        assert created_at_1 == created_at_0, (
            "created_at moved on a live-link self-heal — REGRESSION: this advances "
            "the push window and drops messages that arrived between WebApp opens"
        )

        # Second self-heal → still NO-OP.
        raw2 = make_init_data(telegram_user_id=tg_id)
        r2 = await client.post("/api/telegram/auth", json={"init_data": raw2})
        assert r2.status_code == 200, r2.text
        assert r2.json() == {"linked": False, "healed": True}

        after = await _get_link(db_engine, tg_id)
        assert after is not None
        assert after.created_at == created_at_0, "created_at moved on the 2nd live self-heal"
        assert after.dead_at is None
        assert after.user_id == admin_id

        # NO-OP must not audit at all (no telegram_link_created / rebound).
        assert (
            await _audits(db_engine, "telegram_link_created") == []
        ), "NO-OP self-heal of a live link must NOT write telegram_link_created"
        assert await _audits(db_engine, "telegram_link_rebound") == []


# ---------------------------------------------------------------------------
# Case 3 — dead link, same user → reactivate (dead_at cleared, created_at
# bumped), audit replaced=true via=self_heal.
# ---------------------------------------------------------------------------


class TestSelfHealReactivateDead:
    async def test_dead_link_is_reactivated_with_replaced_true(
        self,
        client: httpx.AsyncClient,
        db_engine: AsyncEngine,
        make_link: Any,
    ) -> None:
        await _login_admin(client)
        admin_id = await _admin_id(db_engine)
        tg_id = 530001

        # Pre-existing DEAD link of the admin.
        await make_link(tg_id, admin_id, dead=True)
        before = await _get_link(db_engine, tg_id)
        assert before is not None and before.dead_at is not None
        created_at_0 = before.created_at

        raw = make_init_data(telegram_user_id=tg_id)
        resp = await client.post("/api/telegram/auth", json={"init_data": raw})
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"linked": False, "healed": True}

        after = await _get_link(db_engine, tg_id)
        assert after is not None
        assert after.dead_at is None, "dead_at must be cleared on reactivation"
        assert after.user_id == admin_id
        assert after.created_at >= created_at_0, "created_at must advance on reactivation"

        created = await _audits(db_engine, "telegram_link_created")
        assert len(created) == 1
        details = created[0].details or {}
        assert details.get("via") == "self_heal"
        assert details.get("replaced") is True


# ---------------------------------------------------------------------------
# Case 4 — link of ANOTHER user → rebound onto the current logged-in user.
# ---------------------------------------------------------------------------


class TestSelfHealRebound:
    async def test_other_users_link_is_rebound_to_current_user(
        self,
        client: httpx.AsyncClient,
        db_engine: AsyncEngine,
        leader_and_group: tuple[Any, User],
        make_link: Any,
    ) -> None:
        """Self-heal carries an implicit proof (the active session), so a TG
        owned by another internal user is REBOUND onto the current user —
        audit ``telegram_link_rebound via=self_heal`` with previous_user_id."""
        _, leader = leader_and_group
        await _login_admin(client)
        admin_id = await _admin_id(db_engine)
        tg_id = 540001

        # TG currently linked to the leader (the "other" user).
        await make_link(tg_id, leader.id)

        raw = make_init_data(telegram_user_id=tg_id)
        resp = await client.post("/api/telegram/auth", json={"init_data": raw})
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"linked": False, "healed": True}

        after = await _get_link(db_engine, tg_id)
        assert after is not None
        assert after.user_id == admin_id, "link must be rebound to the current (admin) user"
        assert after.dead_at is None

        rebound = await _audits(db_engine, "telegram_link_rebound")
        assert len(rebound) == 1
        details = rebound[0].details or {}
        assert details.get("via") == "self_heal"
        assert details.get("previous_user_id") == leader.id
        assert details.get("telegram_user_id") == tg_id


# ---------------------------------------------------------------------------
# Case 5 — router branching: with a session the old SSO flow is bypassed; the
# anonymous responses stay byte-for-byte identical and ``healed`` never leaks.
# ---------------------------------------------------------------------------


class TestRouterBranching:
    async def test_session_present_does_not_create_a_new_session(
        self,
        client: httpx.AsyncClient,
        db_engine: AsyncEngine,
        make_link: Any,
    ) -> None:
        """With a valid session AND a live link for the SAME admin, the
        endpoint must NOT run the legacy ``linked=true`` flow (which would
        issue a brand-new mas_session). It self-heals instead → no Set-Cookie,
        body {linked:false, healed:true}."""
        await _login_admin(client)
        admin_id = await _admin_id(db_engine)
        tg_id = 550001
        # A live link that WOULD make the legacy path return linked=true.
        await make_link(tg_id, admin_id)

        session_before = client.cookies.get("mas_session")

        raw = make_init_data(telegram_user_id=tg_id)
        resp = await client.post("/api/telegram/auth", json={"init_data": raw})
        assert resp.status_code == 200, resp.text
        # Self-heal — NOT the legacy linked=true/redirect=/ response.
        assert resp.json() == {"linked": False, "healed": True}
        # No new session minted.
        set_cookies = resp.headers.get_list("set-cookie")
        assert not any(c.startswith("mas_session=") for c in set_cookies), set_cookies
        # The jar still carries the original session value (unchanged).
        assert client.cookies.get("mas_session") == session_before

    async def test_anonymous_linked_response_is_byte_for_byte_unchanged(
        self,
        client: httpx.AsyncClient,
        super_admin_user: Any,
        make_link: Any,
    ) -> None:
        """No session (anonymous) + active link → legacy linked=true path:
        body is exactly {linked:true, redirect:"/"} with ``healed`` absent."""
        tg_id = 550101
        await make_link(tg_id, super_admin_user.id)
        raw = make_init_data(telegram_user_id=tg_id)
        resp = await client.post("/api/telegram/auth", json={"init_data": raw})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body == {"linked": True, "redirect": "/"}
        assert "healed" not in body, "healed must not leak into anonymous responses"
        assert resp.cookies.get("mas_session") is not None

    async def test_anonymous_unlinked_response_is_byte_for_byte_unchanged(
        self,
        client: httpx.AsyncClient,
    ) -> None:
        """No session + no link → legacy unlinked path: body is exactly
        {linked:false, redirect:"/login"} with ``healed`` absent."""
        tg_id = 550201
        raw = make_init_data(telegram_user_id=tg_id)
        resp = await client.post("/api/telegram/auth", json={"init_data": raw})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body == {"linked": False, "redirect": "/login"}
        assert "healed" not in body, "healed must not leak into anonymous responses"
        assert resp.cookies.get("mas_tg_pending") is not None


# ---------------------------------------------------------------------------
# Case 6 — uniform NO-OP via POST /api/telegram/links (session-add path).
# A repeat upsert of a LIVE link of the same user must NOT bump created_at.
# ---------------------------------------------------------------------------


class TestSessionAddUniformNoOp:
    async def test_repeat_session_add_of_live_link_does_not_move_created_at(
        self,
        client: httpx.AsyncClient,
        db_engine: AsyncEngine,
        make_link: Any,
    ) -> None:
        """POST /api/telegram/links re-linking an already-live TG of the same
        user is a uniform NO-OP (ADR-0022 §1.6): created_at frozen, no second
        ``telegram_link_created`` audit."""
        csrf = await _login_admin(client)
        admin_id = await _admin_id(db_engine)
        tg_id = 560001

        await make_link(tg_id, admin_id)
        before = await _get_link(db_engine, tg_id)
        assert before is not None
        created_at_0 = before.created_at

        raw = make_init_data(telegram_user_id=tg_id)
        resp = await client.post(
            "/api/telegram/links",
            json={"init_data": raw},
            headers={"X-CSRF-Token": csrf},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"linked": True, "telegram_user_id": tg_id}

        after = await _get_link(db_engine, tg_id)
        assert after is not None
        assert after.created_at == created_at_0, (
            "created_at moved on a repeat session-add of a live link — NO-OP rule "
            "must apply uniformly to login_flow / session_add / self_heal"
        )
        # No audit written for the no-op.
        assert await _audits(db_engine, "telegram_link_created") == []
        assert await _audits(db_engine, "telegram_link_rebound") == []


# ---------------------------------------------------------------------------
# Case 7 — best-effort: a self-heal that fails internally must not 500 the
# WebApp. The router returns {linked:false, healed:false}, logs
# telegram_self_heal_failed, and writes nothing.
# ---------------------------------------------------------------------------


class TestSelfHealBestEffort:
    async def test_repo_failure_yields_healed_false_and_no_write(
        self,
        client: httpx.AsyncClient,
        db_engine: AsyncEngine,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When the inner ``_link`` raises (FK race / transient DB error), the
        best-effort wrapper rolls back and returns ``healed=false`` — the
        endpoint still answers 200 (WebApp opens), no link row, no audit."""
        await _login_admin(client)
        tg_id = 570001

        # Force the link write to blow up. We patch the repo PK-read used at
        # the very top of ``_link`` so the failure happens INSIDE the
        # ``self_heal_link`` ``db.begin()`` block (exercising its rollback).
        from backend.app.telegram import sso_service as sso_mod

        async def _boom(self: Any, telegram_user_id: int) -> Any:
            raise RuntimeError("simulated FK race / transient DB error")

        monkeypatch.setattr(
            sso_mod.TelegramLinksRepo,
            "get_by_telegram_user_id",
            _boom,
            raising=True,
        )

        audits_before = await _audit_count(db_engine)

        raw = make_init_data(telegram_user_id=tg_id)
        resp = await client.post("/api/telegram/auth", json={"init_data": raw})

        # Endpoint stays up; self-heal reports failure transparently.
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"linked": False, "healed": False}

        # No partial write: no link row, no new audit entry.
        assert await _get_link(db_engine, tg_id) is None
        assert await _audit_count(db_engine) == audits_before
