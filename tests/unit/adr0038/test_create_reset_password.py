"""ADR-0038 §3 — admin-set password on create / reset, self-set copy, and the
login re-hash invariant.

Covered:
- create_user WITH password → argon2 hash usable for login, ``password_encrypted``
  not NULL, ``has_password=true`` (response + listing), ``password_reset_required``
  false, one ``user_password_set`` audit row;
- create_user WITHOUT password → ``password_encrypted`` NULL, ``has_password``
  false, self-set flow (``password_reset_required`` true, no login yet);
- reset WITH password → hash + reversible copy + ``user_password_set`` audit +
  sessions revoked;
- reset WITHOUT password → force self-set (``password_encrypted`` NULL → "—") +
  sessions revoked;
- self-set (``complete_set_password``) writes ``password_encrypted`` (no de-sync);
- the login argon2 re-hash path (``set_password_hash`` without the kwarg) never
  nulls / clobbers ``password_encrypted``.

Source of truth: ``backend/app/admin/service.py``, ``backend/app/auth/service.py``,
``backend/app/repositories/users.py`` + ADR-0038 §3 + ``docs/04-api-contracts.md`` §4.
"""

from __future__ import annotations

import httpx
import pytest
from argon2 import PasswordHasher
from httpx import ASGITransport
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from backend.app.auth.service import AuthService
from backend.app.repositories.users import UsersRepo
from backend.app.sessions import SetupSessionStore
from shared.crypto import decrypt_user_password

pytestmark = pytest.mark.integration

_LEADER_PW = "LeaderCRPass123"
_MEMBER_PW = "MemberCRPass123"
_PH = PasswordHasher()


async def _login_admin(client: httpx.AsyncClient) -> str:
    from tests.integration.conftest import login_as_admin

    return await login_as_admin(client)


async def _create_leader(client: httpx.AsyncClient, csrf: str, username: str) -> dict:
    resp = await client.post(
        "/api/admin/users",
        json={"username": username, "role": "group_leader", "password": _LEADER_PW},
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
    resp = await client.post("/api/admin/users", json=body, headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _user_row(engine: AsyncEngine, user_id: int) -> dict:
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with factory() as ses:
        row = (
            await ses.execute(
                text(
                    "SELECT password_hash, password_encrypted, password_reset_required "
                    "FROM users WHERE id = :id"
                ),
                {"id": user_id},
            )
        ).one()
    return {
        "password_hash": row[0],
        "password_encrypted": row[1],
        "password_reset_required": row[2],
    }


async def _audit_count(engine: AsyncEngine, action: str, target_user_id: int) -> int:
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with factory() as ses:
        n = await ses.execute(
            text("SELECT count(*) FROM admin_audit " "WHERE action = :a AND target_user_id = :t"),
            {"a": action, "t": target_user_id},
        )
        return int(n.scalar_one())


async def _has_password_in_listing(client: httpx.AsyncClient, user_id: int) -> bool:
    r = await client.get("/api/admin/users?limit=200")
    assert r.status_code == 200, r.text
    for item in r.json()["items"]:
        if item["id"] == user_id:
            return bool(item["has_password"])
    raise AssertionError(f"user {user_id} not found in listing")


# ---------------------------------------------------------------------------
# create_user
# ---------------------------------------------------------------------------


class TestCreateWithPassword:
    async def test_admin_set_password_stores_hash_and_reversible_copy(
        self, client: httpx.AsyncClient, db_engine: AsyncEngine
    ) -> None:
        csrf = await _login_admin(client)
        leader = await _create_leader(client, csrf, "lead_cwp")
        member = await _create_member(client, csrf, "member_cwp", leader["group_id"], _MEMBER_PW)

        assert member["has_password"] is True

        row = await _user_row(db_engine, member["id"])
        assert row["password_hash"] is not None
        assert row["password_encrypted"] is not None
        assert row["password_reset_required"] is False
        # argon2 hash verifies against the plaintext.
        _PH.verify(row["password_hash"], _MEMBER_PW)
        # reversible copy round-trips under this user's AAD.
        assert decrypt_user_password(row["password_encrypted"], member["id"]) == _MEMBER_PW

        # has_password surfaces in the listing DTO too.
        assert await _has_password_in_listing(client, member["id"]) is True
        # exactly one user_password_set audit row.
        assert await _audit_count(db_engine, "user_password_set", member["id"]) == 1

    async def test_admin_set_password_enables_login(self, client: httpx.AsyncClient) -> None:
        csrf = await _login_admin(client)
        leader = await _create_leader(client, csrf, "lead_login")
        await _create_member(client, csrf, "member_login", leader["group_id"], _MEMBER_PW)
        # The member can log in immediately (no forced self-set).
        from tests.integration.conftest import two_step_login

        resp = await two_step_login(client, "member_login", _MEMBER_PW)
        assert resp.status_code in (302, 303), resp.text


class TestCreateWithoutPassword:
    async def test_no_password_leaves_encrypted_null_and_self_set_flow(
        self, client: httpx.AsyncClient, db_engine: AsyncEngine
    ) -> None:
        csrf = await _login_admin(client)
        leader = await _create_leader(client, csrf, "lead_nopw")
        member = await _create_member(client, csrf, "member_nopw", leader["group_id"], None)

        assert member["has_password"] is False
        row = await _user_row(db_engine, member["id"])
        assert row["password_encrypted"] is None
        assert row["password_reset_required"] is True
        assert await _has_password_in_listing(client, member["id"]) is False
        # No user_password_set audit for the self-set flow.
        assert await _audit_count(db_engine, "user_password_set", member["id"]) == 0


# ---------------------------------------------------------------------------
# reset_password
# ---------------------------------------------------------------------------


class TestResetPassword:
    async def test_reset_with_password_sets_hash_encrypted_and_audit(
        self, client: httpx.AsyncClient, db_engine: AsyncEngine
    ) -> None:
        csrf = await _login_admin(client)
        leader = await _create_leader(client, csrf, "lead_rwp")
        member = await _create_member(client, csrf, "member_rwp", leader["group_id"], None)

        new_pw = "ResetNewPass123"
        r = await client.post(
            f"/api/admin/users/{member['id']}/reset",
            json={"password": new_pw},
            headers={"X-CSRF-Token": csrf},
        )
        assert r.status_code == 200, r.text

        row = await _user_row(db_engine, member["id"])
        assert row["password_hash"] is not None
        assert row["password_encrypted"] is not None
        assert row["password_reset_required"] is False
        _PH.verify(row["password_hash"], new_pw)
        assert decrypt_user_password(row["password_encrypted"], member["id"]) == new_pw
        assert await _audit_count(db_engine, "user_password_set", member["id"]) == 1

    async def test_reset_without_password_forces_self_set_and_nulls_copy(
        self, client: httpx.AsyncClient, db_engine: AsyncEngine
    ) -> None:
        csrf = await _login_admin(client)
        leader = await _create_leader(client, csrf, "lead_rnp")
        # Start with an admin-set password so we can prove reset NULLs the copy.
        member = await _create_member(client, csrf, "member_rnp", leader["group_id"], _MEMBER_PW)
        assert (await _user_row(db_engine, member["id"]))["password_encrypted"] is not None
        # The create-with-password already wrote one user_password_set row; the
        # reset-without-password below must not add another.
        before = await _audit_count(db_engine, "user_password_set", member["id"])

        r = await client.post(
            f"/api/admin/users/{member['id']}/reset",
            json={},
            headers={"X-CSRF-Token": csrf},
        )
        assert r.status_code == 200, r.text

        row = await _user_row(db_engine, member["id"])
        assert row["password_hash"] is None
        assert row["password_encrypted"] is None  # column reverts to "—"
        assert row["password_reset_required"] is True
        # No NEW user_password_set audit when no password was provided.
        assert await _audit_count(db_engine, "user_password_set", member["id"]) == before

    async def test_reset_revokes_target_sessions(
        self, app: object, client: httpx.AsyncClient
    ) -> None:
        """After a reset (with or without password) the target's existing
        session must be revoked — a prior authenticated request stops working.
        """
        csrf = await _login_admin(client)
        leader = await _create_leader(client, csrf, "lead_revoke")
        await _create_member(client, csrf, "member_revoke", leader["group_id"], _MEMBER_PW)

        # Log the member in on a *separate* client so its session cookie is
        # isolated from the admin session on ``client``.
        from tests.integration.conftest import two_step_login

        transport = ASGITransport(app=app)  # type: ignore[arg-type]
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", follow_redirects=False
        ) as member_client:
            resp = await two_step_login(member_client, "member_revoke", _MEMBER_PW)
            assert resp.status_code in (302, 303), resp.text
            # Authenticated page works before the reset.
            before = await member_client.get("/accounts")
            assert before.status_code == 200, before.text

            # Look up the member id from the admin listing and reset them.
            listing = await client.get("/api/admin/users?limit=200")
            member_id = next(
                i["id"] for i in listing.json()["items"] if i["username"] == "member_revoke"
            )
            rr = await client.post(
                f"/api/admin/users/{member_id}/reset",
                json={"password": "AfterRevoke123"},
                headers={"X-CSRF-Token": csrf},
            )
            assert rr.status_code == 200, rr.text

            # The member's prior session is now revoked → no longer a 200.
            after = await member_client.get("/accounts")
            assert after.status_code != 200, f"session should be revoked, got {after.status_code}"


# ---------------------------------------------------------------------------
# self-set (complete_set_password) + login re-hash invariant
# ---------------------------------------------------------------------------


class TestSelfSetAndRehash:
    async def test_complete_set_password_writes_reversible_copy(
        self, app: object, client: httpx.AsyncClient, db_engine: AsyncEngine
    ) -> None:
        """When a user sets their own password the reversible copy is written
        alongside the hash — the admin column stays in sync (no de-sync)."""
        csrf = await _login_admin(client)
        leader = await _create_leader(client, csrf, "lead_selfset")
        member = await _create_member(client, csrf, "member_selfset", leader["group_id"], None)
        # Precondition: no reversible copy yet.
        assert (await _user_row(db_engine, member["id"]))["password_encrypted"] is None

        # Drive the self-set flow at the service layer (deterministic; the HTTP
        # form path adds CSRF/rate-limit noise irrelevant to this invariant).
        setup_token, _csrf = await SetupSessionStore().create(member["id"])
        self_pw = "SelfChosenPass123"
        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as ses, ses.begin():
            result = await AuthService(ses).complete_set_password(
                setup_token=setup_token,
                password=self_pw,
                ip="203.0.113.5",
                user_agent="qa-selfset",
            )
        assert result.kind == "session_created"

        row = await _user_row(db_engine, member["id"])
        assert row["password_encrypted"] is not None
        assert row["password_reset_required"] is False
        assert decrypt_user_password(row["password_encrypted"], member["id"]) == self_pw
        _PH.verify(row["password_hash"], self_pw)

    async def test_login_rehash_does_not_clobber_reversible_copy(
        self, client: httpx.AsyncClient, db_engine: AsyncEngine
    ) -> None:
        """The argon2 re-hash path calls ``set_password_hash`` WITHOUT the
        ``password_encrypted`` kwarg — the reversible copy must be preserved,
        never nulled."""
        csrf = await _login_admin(client)
        leader = await _create_leader(client, csrf, "lead_rehash")
        member = await _create_member(client, csrf, "member_rehash", leader["group_id"], _MEMBER_PW)
        original = (await _user_row(db_engine, member["id"]))["password_encrypted"]
        assert original is not None

        # Simulate the login re-hash: update only the hash, leave the copy alone.
        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        new_hash = _PH.hash(_MEMBER_PW)
        async with factory() as ses, ses.begin():
            await UsersRepo(ses).set_password_hash(member["id"], new_hash)

        row = await _user_row(db_engine, member["id"])
        assert row["password_encrypted"] is not None, "re-hash must not NULL the copy"
        assert bytes(row["password_encrypted"]) == bytes(original), "copy must be untouched"
        # And it still decrypts to the same plaintext.
        assert decrypt_user_password(row["password_encrypted"], member["id"]) == _MEMBER_PW
        # Sanity: the copy still corresponds; hash was replaced but verifies.
        _PH.verify(row["password_hash"], _MEMBER_PW)

    async def test_set_password_hash_with_kwarg_updates_copy(
        self, client: httpx.AsyncClient, db_engine: AsyncEngine
    ) -> None:
        """Complement: passing ``password_encrypted`` DOES update the column
        (guards the sentinel logic against a false 'always untouched')."""
        from shared.crypto import encrypt_user_password

        csrf = await _login_admin(client)
        leader = await _create_leader(client, csrf, "lead_kwarg")
        member = await _create_member(client, csrf, "member_kwarg", leader["group_id"], None)
        assert (await _user_row(db_engine, member["id"]))["password_encrypted"] is None

        new_pw = "KwargSetPass123"
        blob = encrypt_user_password(new_pw, member["id"])
        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as ses, ses.begin():
            await UsersRepo(ses).set_password_hash(
                member["id"], _PH.hash(new_pw), password_encrypted=blob
            )
        row = await _user_row(db_engine, member["id"])
        assert row["password_encrypted"] is not None
        assert decrypt_user_password(row["password_encrypted"], member["id"]) == new_pw
