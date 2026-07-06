"""ADR-0038 §4 — ``GET /api/admin/users/{id}/password`` reveal endpoint.

Covered:
- super_admin reveals an admin-set password → 200 ``{"password": ...}``;
- user with ``password_encrypted IS NULL`` → 404 ``password_not_set``;
- non-super_admin (leader / member) → 403;
- non-existent user → 404 ``not_found``;
- per-actor rate-limit (``LIMIT_ADMIN_PASSWORD_REVEAL``) → 429;
- every successful reveal writes a ``user_password_revealed`` audit row whose
  ``details`` never carry the plaintext, and the value is not leaked to logs.

Source of truth: ``backend/app/admin/router.py`` reveal route +
``AdminService.reveal_login_password`` + ADR-0038 §4 +
``docs/04-api-contracts.md`` §4 + ``docs/06-security.md`` §1.15/§2.3.
"""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from shared.models import AdminAudit

pytestmark = pytest.mark.integration

_MEMBER_PW = "MemberLogin123"
_LEADER_PW = "LeaderLogin123"
_TARGET_PW = "RevealTarget999"


async def _login_admin(client: httpx.AsyncClient) -> str:
    from tests.integration.conftest import login_as_admin

    return await login_as_admin(client)


async def _create_leader(client: httpx.AsyncClient, csrf: str, username: str, pw: str) -> dict:
    """Create a group_leader (auto-creates a group) with an admin-set password."""
    resp = await client.post(
        "/api/admin/users",
        json={"username": username, "role": "group_leader", "password": pw},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _create_member(
    client: httpx.AsyncClient, csrf: str, username: str, group_id: int, pw: str | None
) -> dict:
    body: dict = {"username": username, "role": "group_member", "group_id": group_id}
    if pw is not None:
        body["password"] = pw
    resp = await client.post(
        "/api/admin/users",
        json=body,
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _audit_rows(engine: AsyncEngine, action: str) -> list[AdminAudit]:
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with factory() as ses:
        rows = (
            (await ses.execute(select(AdminAudit).where(AdminAudit.action == action)))
            .scalars()
            .all()
        )
        return list(rows)


class TestRevealSuccess:
    async def test_super_admin_reveals_admin_set_password(self, client: httpx.AsyncClient) -> None:
        csrf = await _login_admin(client)
        leader = await _create_leader(client, csrf, "lead_reveal", _LEADER_PW)
        member = await _create_member(client, csrf, "reveal_ok", leader["group_id"], _TARGET_PW)
        r = await client.get(f"/api/admin/users/{member['id']}/password")
        assert r.status_code == 200, r.text
        assert r.json() == {"password": _TARGET_PW}

    async def test_reveal_writes_audit_without_password_value(
        self, client: httpx.AsyncClient, db_engine: AsyncEngine
    ) -> None:
        csrf = await _login_admin(client)
        leader = await _create_leader(client, csrf, "lead_audit", _LEADER_PW)
        member = await _create_member(client, csrf, "reveal_audit", leader["group_id"], _TARGET_PW)
        # Two successful reveals → two audit rows (audit on EACH show).
        await client.get(f"/api/admin/users/{member['id']}/password")
        await client.get(f"/api/admin/users/{member['id']}/password")

        rows = await _audit_rows(db_engine, "user_password_revealed")
        assert len(rows) == 2
        for row in rows:
            assert row.target_user_id == member["id"]
            # details must NOT carry the plaintext.
            details = row.details or {}
            assert _TARGET_PW not in str(details)
            assert "password" not in {k.lower() for k in details}

    async def test_reveal_value_not_in_logs(
        self, client: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        csrf = await _login_admin(client)
        leader = await _create_leader(client, csrf, "lead_log", _LEADER_PW)
        member = await _create_member(client, csrf, "reveal_log", leader["group_id"], _TARGET_PW)
        with caplog.at_level("DEBUG"):
            r = await client.get(f"/api/admin/users/{member['id']}/password")
        assert r.status_code == 200
        # The plaintext must never surface in any captured log line.
        assert _TARGET_PW not in caplog.text


class TestRevealNotSet:
    async def test_null_password_encrypted_returns_404_password_not_set(
        self, client: httpx.AsyncClient
    ) -> None:
        csrf = await _login_admin(client)
        leader = await _create_leader(client, csrf, "lead_notset", _LEADER_PW)
        # Member created WITHOUT a password → password_encrypted stays NULL.
        member = await _create_member(client, csrf, "reveal_notset", leader["group_id"], None)
        r = await client.get(f"/api/admin/users/{member['id']}/password")
        assert r.status_code == 404, r.text
        assert r.json()["error"]["code"] == "password_not_set"

    async def test_nonexistent_user_returns_404_not_found(self, client: httpx.AsyncClient) -> None:
        await _login_admin(client)
        r = await client.get("/api/admin/users/999999/password")
        assert r.status_code == 404, r.text
        assert r.json()["error"]["code"] == "not_found"


class TestRevealForbidden:
    async def test_group_leader_gets_403(self, client: httpx.AsyncClient) -> None:
        csrf = await _login_admin(client)
        leader = await _create_leader(client, csrf, "lead_403", _LEADER_PW)
        member = await _create_member(client, csrf, "target_403", leader["group_id"], _TARGET_PW)
        # Re-login as the leader (non-super_admin). This drops the admin session.
        from tests.integration.conftest import two_step_login

        resp = await two_step_login(client, "lead_403", _LEADER_PW)
        assert resp.status_code in (302, 303), resp.text
        r = await client.get(f"/api/admin/users/{member['id']}/password")
        assert r.status_code == 403, r.text

    async def test_group_member_gets_403(self, client: httpx.AsyncClient) -> None:
        csrf = await _login_admin(client)
        leader = await _create_leader(client, csrf, "lead_m403", _LEADER_PW)
        member = await _create_member(client, csrf, "member_403", leader["group_id"], _MEMBER_PW)
        from tests.integration.conftest import two_step_login

        resp = await two_step_login(client, "member_403", _MEMBER_PW)
        assert resp.status_code in (302, 303), resp.text
        r = await client.get(f"/api/admin/users/{member['id']}/password")
        assert r.status_code == 403, r.text


class TestRevealRateLimit:
    async def test_per_actor_rate_limit_returns_429(self, client: httpx.AsyncClient) -> None:
        """Default capacity is 30 / 60 s per actor. The 31st reveal in the
        window is rejected with 429 — the anti-bulk-exfiltration guard.

        Every request consumes a token *before* the service call, so even a
        404 (no reversible copy) counts toward the limit — we target a
        password-less user to keep the loop cheap.
        """
        from shared.config import get_settings

        capacity = get_settings().ADMIN_PASSWORD_REVEAL_RATE_LIMIT_PER_MINUTE
        csrf = await _login_admin(client)
        leader = await _create_leader(client, csrf, "lead_rl", _LEADER_PW)
        member = await _create_member(client, csrf, "target_rl", leader["group_id"], None)
        statuses: list[int] = []
        for _ in range(capacity + 1):
            r = await client.get(f"/api/admin/users/{member['id']}/password")
            statuses.append(r.status_code)
        # First ``capacity`` requests must not be rate-limited (they 404 —
        # no reversible copy — but that still counts as a consumed token).
        assert statuses[:capacity] == [404] * capacity, statuses
        assert statuses[capacity] == 429, statuses
