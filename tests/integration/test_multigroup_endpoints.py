"""Admin membership endpoints + home-membership invariant (ADR-0030).

End-to-end through the live ASGI app (super_admin session) for:

  POST   /api/admin/users/{id}/groups              add additional membership
  DELETE /api/admin/users/{id}/groups/{group_id}   remove additional membership
  PATCH  /api/admin/users/{id}  {group_id}          "move" home team

Verification (plan §Endpoints + §Инвариант home-membership):
- add -> 201 + new membership row;
- repeat add -> 409 membership_already_exists;
- target super_admin -> 400 cannot_add_super_admin_to_group;
- non-existent group -> 404 group_not_found;
- delete additional -> 204;
- delete home -> 400 cannot_remove_home_membership;
- delete non-existent -> 404 membership_not_found;
- move (PATCH group_id) updates users.group_id AND syncs user_groups;
- move a leader -> 409 cannot_move_group_leader;
- add for a leader -> ok;
- sessions revoked on add/remove/move;
- audit user_group_add / user_group_remove written;
- home-membership invariant: create member/leader, promotion -> exactly one
  home row in user_groups.
"""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

pytestmark = pytest.mark.integration


async def _login_admin(client: httpx.AsyncClient) -> str:
    from tests.integration.conftest import login_as_admin

    return await login_as_admin(client)


async def _create_user(client: httpx.AsyncClient, csrf: str, body: dict) -> dict:
    resp = await client.post("/api/admin/users", json=body, headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _memberships(engine: AsyncEngine, user_id: int) -> set[int]:
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with factory() as ses:
        rows = await ses.execute(
            text("SELECT group_id FROM user_groups WHERE user_id = :uid"), {"uid": user_id}
        )
        return {int(r[0]) for r in rows}


async def _audit_actions(engine: AsyncEngine, action: str) -> int:
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with factory() as ses:
        n = await ses.execute(
            text("SELECT count(*) FROM admin_audit WHERE action = :a"), {"a": action}
        )
        return int(n.scalar_one())


async def _setup_two_groups(client: httpx.AsyncClient, csrf: str) -> dict:
    """Create two leaders (auto-create their groups) + a member in group A.

    Returns ids: leader_a/group_a, leader_b/group_b, member.
    """
    la = await _create_user(client, csrf, {"username": "lead_a", "role": "group_leader"})
    lb = await _create_user(client, csrf, {"username": "lead_b", "role": "group_leader"})
    group_a = la["group_id"]
    group_b = lb["group_id"]
    assert group_a and group_b
    member = await _create_user(
        client, csrf, {"username": "memb_a", "role": "group_member", "group_id": group_a}
    )
    return {
        "leader_a": la["id"],
        "group_a": group_a,
        "leader_b": lb["id"],
        "group_b": group_b,
        "member": member["id"],
    }


class TestAddMembership:
    async def test_add_creates_membership_and_audit(
        self, client: httpx.AsyncClient, db_engine: AsyncEngine
    ) -> None:
        csrf = await _login_admin(client)
        ids = await _setup_two_groups(client, csrf)

        resp = await client.post(
            f"/api/admin/users/{ids['member']}/groups",
            json={"group_id": ids["group_b"]},
            headers={"X-CSRF-Token": csrf},
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["group_id"] == ids["group_b"]
        assert body["user_id"] == ids["member"]

        assert await _memberships(db_engine, ids["member"]) == {ids["group_a"], ids["group_b"]}
        assert await _audit_actions(db_engine, "user_group_add") == 1

    async def test_repeat_add_is_conflict(self, client: httpx.AsyncClient) -> None:
        csrf = await _login_admin(client)
        ids = await _setup_two_groups(client, csrf)
        first = await client.post(
            f"/api/admin/users/{ids['member']}/groups",
            json={"group_id": ids["group_b"]},
            headers={"X-CSRF-Token": csrf},
        )
        assert first.status_code == 201
        again = await client.post(
            f"/api/admin/users/{ids['member']}/groups",
            json={"group_id": ids["group_b"]},
            headers={"X-CSRF-Token": csrf},
        )
        assert again.status_code == 409, again.text
        assert again.json()["error"]["code"] == "membership_already_exists"

    async def test_add_super_admin_target_rejected(
        self, client: httpx.AsyncClient, db_engine: AsyncEngine
    ) -> None:
        csrf = await _login_admin(client)
        ids = await _setup_two_groups(client, csrf)
        # Find the seeded super-admin id.
        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as ses:
            sa_id = int(
                (
                    await ses.execute(
                        text("SELECT id FROM users WHERE role = 'super_admin' LIMIT 1")
                    )
                ).scalar_one()
            )
        resp = await client.post(
            f"/api/admin/users/{sa_id}/groups",
            json={"group_id": ids["group_a"]},
            headers={"X-CSRF-Token": csrf},
        )
        assert resp.status_code == 400, resp.text
        assert resp.json()["error"]["code"] == "cannot_add_super_admin_to_group"

    async def test_add_nonexistent_group_404(self, client: httpx.AsyncClient) -> None:
        csrf = await _login_admin(client)
        ids = await _setup_two_groups(client, csrf)
        resp = await client.post(
            f"/api/admin/users/{ids['member']}/groups",
            json={"group_id": 999999},
            headers={"X-CSRF-Token": csrf},
        )
        assert resp.status_code == 404, resp.text
        assert resp.json()["error"]["code"] == "group_not_found"

    async def test_add_for_leader_ok(
        self, client: httpx.AsyncClient, db_engine: AsyncEngine
    ) -> None:
        csrf = await _login_admin(client)
        ids = await _setup_two_groups(client, csrf)
        # Leader A gets an ADDITIONAL membership in group B (allowed).
        resp = await client.post(
            f"/api/admin/users/{ids['leader_a']}/groups",
            json={"group_id": ids["group_b"]},
            headers={"X-CSRF-Token": csrf},
        )
        assert resp.status_code == 201, resp.text
        assert await _memberships(db_engine, ids["leader_a"]) == {ids["group_a"], ids["group_b"]}


class TestRemoveMembership:
    async def test_remove_additional_membership_204(
        self, client: httpx.AsyncClient, db_engine: AsyncEngine
    ) -> None:
        csrf = await _login_admin(client)
        ids = await _setup_two_groups(client, csrf)
        await client.post(
            f"/api/admin/users/{ids['member']}/groups",
            json={"group_id": ids["group_b"]},
            headers={"X-CSRF-Token": csrf},
        )
        resp = await client.delete(
            f"/api/admin/users/{ids['member']}/groups/{ids['group_b']}",
            headers={"X-CSRF-Token": csrf},
        )
        assert resp.status_code == 204, resp.text
        assert await _memberships(db_engine, ids["member"]) == {ids["group_a"]}
        assert await _audit_actions(db_engine, "user_group_remove") == 1

    async def test_remove_home_membership_rejected(self, client: httpx.AsyncClient) -> None:
        csrf = await _login_admin(client)
        ids = await _setup_two_groups(client, csrf)
        resp = await client.delete(
            f"/api/admin/users/{ids['member']}/groups/{ids['group_a']}",
            headers={"X-CSRF-Token": csrf},
        )
        assert resp.status_code == 400, resp.text
        assert resp.json()["error"]["code"] == "cannot_remove_home_membership"

    async def test_remove_nonexistent_membership_404(self, client: httpx.AsyncClient) -> None:
        csrf = await _login_admin(client)
        ids = await _setup_two_groups(client, csrf)
        resp = await client.delete(
            f"/api/admin/users/{ids['member']}/groups/{ids['group_b']}",
            headers={"X-CSRF-Token": csrf},
        )
        assert resp.status_code == 404, resp.text
        assert resp.json()["error"]["code"] == "membership_not_found"


class TestMove:
    async def test_move_member_updates_home_and_syncs_memberships(
        self, client: httpx.AsyncClient, db_engine: AsyncEngine
    ) -> None:
        csrf = await _login_admin(client)
        ids = await _setup_two_groups(client, csrf)
        # Move member from A (home) to B.
        resp = await client.patch(
            f"/api/admin/users/{ids['member']}",
            json={"group_id": ids["group_b"]},
            headers={"X-CSRF-Token": csrf},
        )
        assert resp.status_code == 200, resp.text

        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as ses:
            home = int(
                (
                    await ses.execute(
                        text("SELECT group_id FROM users WHERE id = :uid"),
                        {"uid": ids["member"]},
                    )
                ).scalar_one()
            )
        assert home == ids["group_b"], "users.group_id (home) updated"
        # Old home membership dropped, new home present.
        assert await _memberships(db_engine, ids["member"]) == {ids["group_b"]}
        assert await _audit_actions(db_engine, "user_group_change") == 1

    async def test_move_keeps_additional_memberships(
        self, client: httpx.AsyncClient, db_engine: AsyncEngine
    ) -> None:
        """Moving home A->B with an existing additional membership in B must
        dedup (end state = {B}); a different additional team survives.
        """
        csrf = await _login_admin(client)
        ids = await _setup_two_groups(client, csrf)
        # third group via another leader
        lc = await _create_user(client, csrf, {"username": "lead_c", "role": "group_leader"})
        group_c = lc["group_id"]
        # member gets additional membership in C
        await client.post(
            f"/api/admin/users/{ids['member']}/groups",
            json={"group_id": group_c},
            headers={"X-CSRF-Token": csrf},
        )
        # move home A -> B
        resp = await client.patch(
            f"/api/admin/users/{ids['member']}",
            json={"group_id": ids["group_b"]},
            headers={"X-CSRF-Token": csrf},
        )
        assert resp.status_code == 200, resp.text
        # old home A gone, new home B added, additional C kept.
        assert await _memberships(db_engine, ids["member"]) == {ids["group_b"], group_c}

    async def test_move_leader_rejected(self, client: httpx.AsyncClient) -> None:
        csrf = await _login_admin(client)
        ids = await _setup_two_groups(client, csrf)
        resp = await client.patch(
            f"/api/admin/users/{ids['leader_a']}",
            json={"group_id": ids["group_b"]},
            headers={"X-CSRF-Token": csrf},
        )
        assert resp.status_code == 409, resp.text
        assert resp.json()["error"]["code"] == "cannot_move_group_leader"


class TestSessionRevocation:
    async def test_add_revokes_target_sessions(
        self, client: httpx.AsyncClient, db_engine: AsyncEngine
    ) -> None:
        """After add membership, the target's existing sessions are revoked so
        the scope is re-read (ADR-0030 / ADR-0019 §10).
        """
        from backend.app.sessions import SessionStore

        csrf = await _login_admin(client)
        ids = await _setup_two_groups(client, csrf)

        # Create a session for the member directly via the store.
        store = SessionStore()
        token, _ = await store.create(
            ids["member"],
            "group_member",
            ids["group_a"],
            "1.2.3.4",
            "x",
        )
        assert await store.get(token) is not None

        resp = await client.post(
            f"/api/admin/users/{ids['member']}/groups",
            json={"group_id": ids["group_b"]},
            headers={"X-CSRF-Token": csrf},
        )
        assert resp.status_code == 201, resp.text
        assert await store.get(token) is None, "member's session must be revoked on add"


class TestHomeMembershipInvariant:
    async def test_created_member_has_exactly_one_home_membership(
        self, client: httpx.AsyncClient, db_engine: AsyncEngine
    ) -> None:
        csrf = await _login_admin(client)
        ids = await _setup_two_groups(client, csrf)
        assert await _memberships(db_engine, ids["member"]) == {ids["group_a"]}

    async def test_created_leader_has_exactly_one_home_membership(
        self, client: httpx.AsyncClient, db_engine: AsyncEngine
    ) -> None:
        csrf = await _login_admin(client)
        la = await _create_user(client, csrf, {"username": "solo_lead", "role": "group_leader"})
        assert await _memberships(db_engine, la["id"]) == {la["group_id"]}

    async def test_promotion_to_leader_keeps_single_home_membership(
        self, client: httpx.AsyncClient, db_engine: AsyncEngine
    ) -> None:
        """Promote a group_member to group_leader (auto-creates a new group).
        After promotion the user must have exactly one home membership = the
        new group, and the old home membership must be gone.
        """
        csrf = await _login_admin(client)
        ids = await _setup_two_groups(client, csrf)
        before = await _memberships(db_engine, ids["member"])
        assert before == {ids["group_a"]}

        resp = await client.patch(
            f"/api/admin/users/{ids['member']}",
            json={"role": "group_leader"},
            headers={"X-CSRF-Token": csrf},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        new_group = body["group"]["id"]
        assert new_group != ids["group_a"], "promotion auto-creates a fresh group"
        memberships = await _memberships(db_engine, ids["member"])
        assert memberships == {new_group}, "exactly one home membership = new led group"
