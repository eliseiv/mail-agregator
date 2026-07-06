"""ADR-0038 §5 / ADR-0030 — ``additional_group_ids`` on user creation.

Covered:
- group_member with home + 2 additional teams → exactly 3 ``user_groups`` rows;
- dedup: the home team and repeats among the additional list collapse;
- a non-existent additional team → 400 ``group_not_found`` (field
  ``additional_group_ids``) AND a full rollback (no user, no partial memberships);
- group_leader → additional teams ignored (only the home/auto team remains);
- one ``user_group_add`` audit row per team actually added.

Source of truth: ``backend/app/admin/service.py`` (`_add_additional_memberships`)
+ ADR-0038 §5 + ADR-0030 + ``docs/04-api-contracts.md`` §4.
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


async def _create_leader(client: httpx.AsyncClient, csrf: str, username: str) -> dict:
    resp = await client.post(
        "/api/admin/users",
        json={"username": username, "role": "group_leader"},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _create_member(client: httpx.AsyncClient, csrf: str, body: dict) -> httpx.Response:
    return await client.post("/api/admin/users", json=body, headers={"X-CSRF-Token": csrf})


async def _memberships(engine: AsyncEngine, user_id: int) -> set[int]:
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with factory() as ses:
        rows = await ses.execute(
            text("SELECT group_id FROM user_groups WHERE user_id = :uid"), {"uid": user_id}
        )
        return {int(r[0]) for r in rows}


async def _username_exists(engine: AsyncEngine, username: str) -> bool:
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with factory() as ses:
        n = await ses.execute(
            text("SELECT count(*) FROM users WHERE username = :u"), {"u": username}
        )
        return int(n.scalar_one()) > 0


async def _user_group_add_count(engine: AsyncEngine, target_username: str) -> int:
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with factory() as ses:
        n = await ses.execute(
            text(
                "SELECT count(*) FROM admin_audit "
                "WHERE action = 'user_group_add' AND target_username = :u"
            ),
            {"u": target_username},
        )
        return int(n.scalar_one())


class TestAdditionalGroupsHappyPath:
    async def test_member_with_home_plus_two_additional_gets_three_memberships(
        self, client: httpx.AsyncClient, db_engine: AsyncEngine
    ) -> None:
        csrf = await _login_admin(client)
        a = await _create_leader(client, csrf, "lead_ag_a")
        b = await _create_leader(client, csrf, "lead_ag_b")
        c = await _create_leader(client, csrf, "lead_ag_c")

        resp = await _create_member(
            client,
            csrf,
            {
                "username": "member_three",
                "role": "group_member",
                "group_id": a["group_id"],
                "additional_group_ids": [b["group_id"], c["group_id"]],
            },
        )
        assert resp.status_code == 201, resp.text
        member_id = resp.json()["id"]

        got = await _memberships(db_engine, member_id)
        assert got == {a["group_id"], b["group_id"], c["group_id"]}
        # One user_group_add per additional team actually added (home is not
        # an "add").
        assert await _user_group_add_count(db_engine, "member_three") == 2

    async def test_dedup_home_and_repeated_additional(
        self, client: httpx.AsyncClient, db_engine: AsyncEngine
    ) -> None:
        csrf = await _login_admin(client)
        a = await _create_leader(client, csrf, "lead_dd_a")
        b = await _create_leader(client, csrf, "lead_dd_b")

        # additional = [home, b, b] → only b is a real add; result = {a, b}.
        resp = await _create_member(
            client,
            csrf,
            {
                "username": "member_dedup",
                "role": "group_member",
                "group_id": a["group_id"],
                "additional_group_ids": [a["group_id"], b["group_id"], b["group_id"]],
            },
        )
        assert resp.status_code == 201, resp.text
        member_id = resp.json()["id"]
        assert await _memberships(db_engine, member_id) == {a["group_id"], b["group_id"]}
        assert await _user_group_add_count(db_engine, "member_dedup") == 1


class TestAdditionalGroupsValidationAndRollback:
    async def test_nonexistent_additional_team_400_and_rollback(
        self, client: httpx.AsyncClient, db_engine: AsyncEngine
    ) -> None:
        csrf = await _login_admin(client)
        a = await _create_leader(client, csrf, "lead_bad_a")

        resp = await _create_member(
            client,
            csrf,
            {
                "username": "member_rollback",
                "role": "group_member",
                "group_id": a["group_id"],
                "additional_group_ids": [a["group_id"], 999999],
            },
        )
        assert resp.status_code == 400, resp.text
        err = resp.json()["error"]
        assert err["code"] == "validation_error"
        assert err["field"] == "additional_group_ids"
        assert err.get("details", {}).get("group_id") == 999999

        # Full rollback: the user was never created, so no membership rows.
        assert await _username_exists(db_engine, "member_rollback") is False


class TestAdditionalGroupsIgnoredForLeaders:
    async def test_leader_ignores_additional_group_ids(
        self, client: httpx.AsyncClient, db_engine: AsyncEngine
    ) -> None:
        csrf = await _login_admin(client)
        b = await _create_leader(client, csrf, "lead_extra_b")

        # Create a leader (auto-creates its own group) but pass additional teams
        # — they must be ignored for the leader role.
        resp = await client.post(
            "/api/admin/users",
            json={
                "username": "lead_ignore",
                "role": "group_leader",
                "additional_group_ids": [b["group_id"]],
            },
            headers={"X-CSRF-Token": csrf},
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        leader_id = body["id"]
        # Only the home/auto team — the additional team was ignored.
        assert await _memberships(db_engine, leader_id) == {body["group_id"]}
        assert b["group_id"] not in await _memberships(db_engine, leader_id)
