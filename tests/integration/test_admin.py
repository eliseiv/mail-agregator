"""Integration tests for /api/admin endpoints.

Covers create_user / reset_password / delete_user + audit list.

Source of truth: ``backend/app/admin/router.py`` + ``service.py``,
``docs/04-api-contracts.md`` sec.6, ``docs/05-modules.md`` sec.8.
"""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from shared.config import get_settings
from shared.models import AdminAudit, User

pytestmark = pytest.mark.integration


async def _login_admin(client: httpx.AsyncClient) -> str:
    s = get_settings()
    resp = await client.post(
        "/login",
        data={"username": s.ADMIN_LOGIN, "password": s.ADMIN_PASSWORD},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert resp.status_code == 302
    return resp.cookies["mas_csrf"]


class TestCreateUser:
    async def test_create_user_via_json(
        self, client: httpx.AsyncClient, db_engine: AsyncEngine
    ) -> None:
        csrf = await _login_admin(client)
        resp = await client.post(
            "/api/admin/users",
            json={"username": "alice", "email": "alice@x.com"},
            headers={"X-CSRF-Token": csrf},
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["username"] == "alice"
        assert body["email"] == "alice@x.com"
        # Audit row written.
        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as ses:
            audits = (
                (await ses.execute(select(AdminAudit).where(AdminAudit.action == "create_user")))
                .scalars()
                .all()
            )
        assert len(audits) == 1
        assert audits[0].target_username == "alice"

    async def test_create_user_via_form_redirects(self, client: httpx.AsyncClient) -> None:
        csrf = await _login_admin(client)
        resp = await client.post(
            "/api/admin/users",
            data={"csrf_token": csrf, "username": "bob", "email": ""},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/admin"

    async def test_duplicate_username_returns_409(self, client: httpx.AsyncClient) -> None:
        csrf = await _login_admin(client)
        body = {"username": "carol", "email": None}
        a = await client.post("/api/admin/users", json=body, headers={"X-CSRF-Token": csrf})
        assert a.status_code == 201
        b = await client.post("/api/admin/users", json=body, headers={"X-CSRF-Token": csrf})
        assert b.status_code == 409
        assert b.json()["error"]["code"] == "conflict"

    async def test_username_validation_rejects_special_chars(
        self, client: httpx.AsyncClient
    ) -> None:
        csrf = await _login_admin(client)
        resp = await client.post(
            "/api/admin/users",
            json={"username": "has spaces", "email": None},
            headers={"X-CSRF-Token": csrf},
        )
        assert resp.status_code in (400, 422)


class TestResetPassword:
    async def test_reset_clears_hash_and_writes_audit(
        self, client: httpx.AsyncClient, db_engine: AsyncEngine
    ) -> None:
        csrf = await _login_admin(client)
        a = await client.post(
            "/api/admin/users",
            json={"username": "dave", "email": None},
            headers={"X-CSRF-Token": csrf},
        )
        target_id = a.json()["id"]
        # Bump password (simulate user set one) directly via SQL so we can
        # verify reset clears it.
        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as ses, ses.begin():
            user = await ses.get(User, target_id)
            assert user is not None
            user.password_hash = "$argon2id$v=19$m=65536,t=3,p=4$abc$def"
            user.password_reset_required = False

        # Now reset.
        r = await client.post(
            f"/api/admin/users/{target_id}/reset",
            headers={"X-CSRF-Token": csrf},
        )
        assert r.status_code == 200, r.text
        # Re-fetch user.
        async with factory() as ses:
            user = await ses.get(User, target_id)
            assert user is not None
            assert user.password_hash is None
            assert user.password_reset_required is True

        # Audit written.
        async with factory() as ses:
            audits = (
                (await ses.execute(select(AdminAudit).where(AdminAudit.action == "reset_password")))
                .scalars()
                .all()
            )
        assert len(audits) == 1

    async def test_reset_admin_self_rejected(
        self, client: httpx.AsyncClient, db_engine: AsyncEngine
    ) -> None:
        csrf = await _login_admin(client)
        # Find super-admin id.
        s = get_settings()
        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as ses:
            admin = (
                await ses.execute(select(User).where(User.username == s.ADMIN_LOGIN))
            ).scalar_one()
        resp = await client.post(
            f"/api/admin/users/{admin.id}/reset",
            headers={"X-CSRF-Token": csrf},
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "cannot_reset_admin"


class TestDeleteUser:
    async def test_delete_writes_audit_and_cascades(
        self, client: httpx.AsyncClient, db_engine: AsyncEngine
    ) -> None:
        csrf = await _login_admin(client)
        a = await client.post(
            "/api/admin/users",
            json={"username": "eve", "email": None},
            headers={"X-CSRF-Token": csrf},
        )
        target_id = a.json()["id"]
        r = await client.delete(
            f"/api/admin/users/{target_id}",
            headers={"X-CSRF-Token": csrf},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True

        # User row gone.
        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as ses:
            assert await ses.get(User, target_id) is None
            audits = (
                (await ses.execute(select(AdminAudit).where(AdminAudit.action == "delete_user")))
                .scalars()
                .all()
            )
        assert len(audits) == 1

    async def test_delete_admin_rejected(
        self, client: httpx.AsyncClient, db_engine: AsyncEngine
    ) -> None:
        csrf = await _login_admin(client)
        s = get_settings()
        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as ses:
            admin = (
                await ses.execute(select(User).where(User.username == s.ADMIN_LOGIN))
            ).scalar_one()
        resp = await client.delete(
            f"/api/admin/users/{admin.id}",
            headers={"X-CSRF-Token": csrf},
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "cannot_delete_admin"


class TestAudit:
    async def test_audit_list_pagination(self, client: httpx.AsyncClient) -> None:
        csrf = await _login_admin(client)
        # Create 3 users -> 3 audit rows + 1 admin_login. Usernames must be
        # ``min_length=3`` per ``CreateUserRequest`` (admin/schemas.py) — using
        # 2-char names like "u1" produced 400s and broke the audit-count
        # assertion below; expanded to 3 chars so the POSTs succeed.
        for u in ("usr1", "usr2", "usr3"):
            r = await client.post(
                "/api/admin/users",
                json={"username": u, "email": None},
                headers={"X-CSRF-Token": csrf},
            )
            assert (
                r.status_code == 201
            ), f"create_user setup failed for {u}: {r.status_code} {r.text[:200]}"
        resp = await client.get("/api/admin/audit?page=1&limit=2")
        assert resp.status_code == 200
        body = resp.json()
        assert "items" in body
        assert "total" in body
        assert body["page"] == 1
        assert body["limit"] == 2
        assert len(body["items"]) <= 2
        assert body["total"] >= 4

    async def test_non_admin_blocked_from_admin_endpoints(
        self, client: httpx.AsyncClient, db_engine: AsyncEngine
    ) -> None:
        # Create a non-admin user, log in as them, ensure /api/admin/* is 403.
        csrf = await _login_admin(client)
        await client.post(
            "/api/admin/users",
            json={"username": "regular", "email": None},
            headers={"X-CSRF-Token": csrf},
        )
        # Set their password directly.
        from argon2 import PasswordHasher

        ph = PasswordHasher()
        new_hash = ph.hash("regular-pwd-12345!")
        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as ses, ses.begin():
            user = (await ses.execute(select(User).where(User.username == "regular"))).scalar_one()
            user.password_hash = new_hash
            user.password_reset_required = False

        # Logout admin.
        await client.post("/logout", headers={"X-CSRF-Token": csrf})
        client.cookies.clear()

        # Login as regular.
        login = await client.post(
            "/login",
            data={"username": "regular", "password": "regular-pwd-12345!"},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert login.status_code == 302

        resp = await client.get("/api/admin/users")
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "forbidden"
