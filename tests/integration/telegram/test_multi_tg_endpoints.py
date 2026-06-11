"""ADR-0024 §4 (Sprint A) — multi-link management endpoints (items G, E, H).

``GET /api/telegram/links`` / ``POST /api/telegram/links`` /
``DELETE /api/telegram/links/{tg}``. All cookie-authenticated + CSRF-protected
(only ``/api/telegram/auth`` and the webhook prefix are CSRF-exempt).

G. list returns the current user's links; add binds a fresh TG (valid HMAC);
   delete removes one (siblings live), deleting a missing/foreign TG returns
   ``{"deleted": false}`` (never 404 — no ownership leak); auth required (401
   without a session); CSRF required on state-changing verbs.
E. add at the soft limit → 409 ``tg_link_limit`` + audit
   ``telegram_link_limit_reached``.
H. logout / admin-reset revoke ALL links with a single
   ``telegram_link_revoked`` audit carrying ``details.telegram_user_ids=[...]``.
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


async def _login_admin(client: httpx.AsyncClient) -> str:
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
    csrf = resp.cookies.get("mas_csrf") or client.cookies.get("mas_csrf")
    assert csrf, resp.text
    return csrf


async def _admin_id(db_engine: AsyncEngine) -> int:
    s = get_settings()
    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with factory() as ses:
        admin = (await ses.execute(select(User).where(User.username == s.ADMIN_LOGIN))).scalar_one()
        return admin.id


# ---------------------------------------------------------------------------
# G. GET /api/telegram/links
# ---------------------------------------------------------------------------


class TestListLinks:
    async def test_list_returns_current_user_links(
        self,
        client: httpx.AsyncClient,
        db_engine: AsyncEngine,
        make_link: Any,
    ) -> None:
        admin_id = await _admin_id(db_engine)
        await make_link(430001, admin_id)
        await make_link(430002, admin_id)
        await _login_admin(client)

        resp = await client.get("/api/telegram/links")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        tg_ids = {item["telegram_user_id"] for item in body["links"]}
        assert tg_ids == {430001, 430002}
        assert body["max"] == get_settings().TG_MAX_LINKS_PER_USER

    async def test_list_only_returns_own_links_not_other_users(
        self,
        client: httpx.AsyncClient,
        db_engine: AsyncEngine,
        leader_and_group: tuple[Any, User],
        make_link: Any,
    ) -> None:
        admin_id = await _admin_id(db_engine)
        _, leader = leader_and_group
        await make_link(430101, admin_id)
        await make_link(430102, leader.id)  # belongs to someone else
        await _login_admin(client)

        resp = await client.get("/api/telegram/links")
        assert resp.status_code == 200
        tg_ids = {item["telegram_user_id"] for item in resp.json()["links"]}
        assert tg_ids == {430101}, "must not leak another user's links"

    async def test_list_requires_session(self, client: httpx.AsyncClient) -> None:
        resp = await client.get("/api/telegram/links")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# G + E. POST /api/telegram/links
# ---------------------------------------------------------------------------


class TestAddLink:
    async def test_add_binds_fresh_tg_to_session(
        self,
        client: httpx.AsyncClient,
        db_engine: AsyncEngine,
    ) -> None:
        admin_id = await _admin_id(db_engine)
        csrf = await _login_admin(client)
        raw = make_init_data(telegram_user_id=440001)

        resp = await client.post(
            "/api/telegram/links",
            json={"init_data": raw},
            headers={"X-CSRF-Token": csrf},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"linked": True, "telegram_user_id": 440001}

        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as ses:
            link = (
                await ses.execute(
                    select(TelegramLink).where(TelegramLink.telegram_user_id == 440001)
                )
            ).scalar_one()
            assert link.user_id == admin_id

    async def test_add_requires_session(self, client: httpx.AsyncClient) -> None:
        raw = make_init_data(telegram_user_id=440101)
        resp = await client.post("/api/telegram/links", json={"init_data": raw})
        # No session → 401 (auth runs before CSRF for this dep ordering, but
        # either way it is rejected without a valid session).
        assert resp.status_code in (401, 403)

    async def test_add_without_csrf_is_rejected(
        self,
        client: httpx.AsyncClient,
    ) -> None:
        await _login_admin(client)
        raw = make_init_data(telegram_user_id=440201)
        # Omit the X-CSRF-Token header on a state-changing POST.
        resp = await client.post("/api/telegram/links", json={"init_data": raw})
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "csrf_failed"

    async def test_add_at_limit_returns_409_tg_link_limit(
        self,
        client: httpx.AsyncClient,
        db_engine: AsyncEngine,
        make_link: Any,
        monkeypatch: Any,
    ) -> None:
        """At the soft cap, ``POST /api/telegram/links`` must return 409
        ``tg_link_limit`` and create no link (ADR-0024 §3/§4)."""
        monkeypatch.setenv("TG_MAX_LINKS_PER_USER", "2")
        get_settings.cache_clear()
        admin_id = await _admin_id(db_engine)
        await make_link(440301, admin_id)
        await make_link(440302, admin_id)  # at the cap of 2

        csrf = await _login_admin(client)
        raw = make_init_data(telegram_user_id=440399)
        resp = await client.post(
            "/api/telegram/links",
            json={"init_data": raw},
            headers={"X-CSRF-Token": csrf},
        )
        assert resp.status_code == 409, resp.text
        assert resp.json()["error"]["code"] == "tg_link_limit"

        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as ses:
            n = int(
                (
                    await ses.execute(
                        select(func.count())
                        .select_from(TelegramLink)
                        .where(TelegramLink.telegram_user_id == 440399)
                    )
                ).scalar_one()
            )
            assert n == 0
        get_settings.cache_clear()

    async def test_add_other_users_tg_returns_409_owned_by_other(
        self,
        client: httpx.AsyncClient,
        db_engine: AsyncEngine,
        leader_and_group: tuple[Any, User],
        make_link: Any,
    ) -> None:
        _, leader = leader_and_group
        await make_link(440401, leader.id)
        csrf = await _login_admin(client)
        raw = make_init_data(telegram_user_id=440401)
        resp = await client.post(
            "/api/telegram/links",
            json={"init_data": raw},
            headers={"X-CSRF-Token": csrf},
        )
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "tg_link_owned_by_other"


# ---------------------------------------------------------------------------
# G. DELETE /api/telegram/links/{tg}
# ---------------------------------------------------------------------------


class TestDeleteLink:
    async def test_delete_one_keeps_siblings(
        self,
        client: httpx.AsyncClient,
        db_engine: AsyncEngine,
        make_link: Any,
    ) -> None:
        admin_id = await _admin_id(db_engine)
        await make_link(450001, admin_id)
        await make_link(450002, admin_id)
        csrf = await _login_admin(client)

        resp = await client.delete("/api/telegram/links/450001", headers={"X-CSRF-Token": csrf})
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"deleted": True}

        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as ses:
            remaining = (
                (await ses.execute(select(TelegramLink).where(TelegramLink.user_id == admin_id)))
                .scalars()
                .all()
            )
            assert {link.telegram_user_id for link in remaining} == {450002}

    async def test_delete_nonexistent_returns_deleted_false(
        self,
        client: httpx.AsyncClient,
    ) -> None:
        csrf = await _login_admin(client)
        resp = await client.delete("/api/telegram/links/459999", headers={"X-CSRF-Token": csrf})
        assert resp.status_code == 200
        assert resp.json() == {"deleted": False}

    async def test_delete_other_users_link_returns_deleted_false(
        self,
        client: httpx.AsyncClient,
        db_engine: AsyncEngine,
        leader_and_group: tuple[Any, User],
        make_link: Any,
    ) -> None:
        """Deleting a TG owned by another user must report ``deleted=false``
        (idempotent, no ownership leak) and leave the foreign link intact."""
        _, leader = leader_and_group
        await make_link(450101, leader.id)
        csrf = await _login_admin(client)

        resp = await client.delete("/api/telegram/links/450101", headers={"X-CSRF-Token": csrf})
        assert resp.status_code == 200
        assert resp.json() == {"deleted": False}

        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as ses:
            link = (
                await ses.execute(
                    select(TelegramLink).where(TelegramLink.telegram_user_id == 450101)
                )
            ).scalar_one_or_none()
            assert link is not None and link.user_id == leader.id

    async def test_delete_requires_session(self, client: httpx.AsyncClient) -> None:
        resp = await client.delete("/api/telegram/links/450201")
        assert resp.status_code in (401, 403)

    async def test_delete_without_csrf_is_rejected(
        self,
        client: httpx.AsyncClient,
        db_engine: AsyncEngine,
        make_link: Any,
    ) -> None:
        admin_id = await _admin_id(db_engine)
        await make_link(450301, admin_id)
        await _login_admin(client)
        resp = await client.delete("/api/telegram/links/450301")  # no CSRF header
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "csrf_failed"


# ---------------------------------------------------------------------------
# H. round-43 (ADR-0022 §1.5): logout KEEPS all links (decoupled); admin-reset
#    still revokes ALL links with a single array audit (forced revoke path).
# ---------------------------------------------------------------------------


class TestRevokeAllAudit:
    async def test_logout_keeps_all_links_and_writes_no_revoke_audit(
        self,
        client: httpx.AsyncClient,
        db_engine: AsyncEngine,
        make_link: Any,
    ) -> None:
        """round-43: logout no longer revokes ``telegram_links``. A
        multi-linked user keeps ALL chats alive after logout, and NO
        ``telegram_link_revoked`` audit is written for the logout."""
        admin_id = await _admin_id(db_engine)
        await make_link(460001, admin_id)
        await make_link(460002, admin_id)
        csrf = await _login_admin(client)

        resp = await client.post("/logout", headers={"X-CSRF-Token": csrf})
        assert resp.status_code in (302, 303), resp.text

        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as ses:
            # round-43: ALL links survive logout.
            remaining = (
                (await ses.execute(select(TelegramLink).where(TelegramLink.user_id == admin_id)))
                .scalars()
                .all()
            )
            assert {link.telegram_user_id for link in remaining} == {460001, 460002}
            assert all(link.dead_at is None for link in remaining)

            # No revoke audit for logout.
            n_revokes = int(
                (
                    await ses.execute(
                        select(func.count())
                        .select_from(AdminAudit)
                        .where(AdminAudit.action == "telegram_link_revoked")
                    )
                ).scalar_one()
            )
            assert n_revokes == 0

    async def test_admin_reset_revokes_all_target_links(
        self,
        client: httpx.AsyncClient,
        db_engine: AsyncEngine,
        leader_and_group: tuple[Any, User],
        make_link: Any,
    ) -> None:
        _, leader = leader_and_group
        await make_link(460101, leader.id)
        await make_link(460102, leader.id)
        csrf = await _login_admin(client)

        resp = await client.post(
            f"/api/admin/users/{leader.id}/reset",
            headers={"X-CSRF-Token": csrf},
        )
        assert resp.status_code == 200, resp.text

        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as ses:
            n = int(
                (
                    await ses.execute(
                        select(func.count())
                        .select_from(TelegramLink)
                        .where(TelegramLink.user_id == leader.id)
                    )
                ).scalar_one()
            )
            assert n == 0
            revokes = (
                (
                    await ses.execute(
                        select(AdminAudit).where(AdminAudit.action == "telegram_link_revoked")
                    )
                )
                .scalars()
                .all()
            )
            assert len(revokes) == 1
            ids = (revokes[0].details or {}).get("telegram_user_ids")
            assert ids is not None
            assert sorted(ids) == [460101, 460102]
            assert (revokes[0].details or {}).get("reason") == "password_reset"
