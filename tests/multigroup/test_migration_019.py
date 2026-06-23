"""Migration 019 (user_groups) — schema, backfill, downgrade (ADR-0030).

Runs the migration in an isolated throwaway DB on the same Postgres server so
the shared test DB other tests rely on is never disturbed (same technique as
``tests/oauth/test_migration_018_downgrade.py``).

Verification (from the plan's §Verification — Миграция/Backfill):

1. ``upgrade 018 -> 019`` creates ``user_groups`` (PK, UNIQUE, index, FKs).
2. **Backfill**: for every ``users.group_id IS NOT NULL`` there is exactly one
   ``user_groups(user_id, group_id)`` row.
3. **super_admin** (``group_id IS NULL``) gets **no** ``user_groups`` row.
4. ``downgrade 019 -> 018`` drops ``user_groups`` and **preserves**
   ``users.group_id`` (the column is never removed by 019).
5. Migration replays cleanly (up -> down -> up).
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import uuid
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest

from shared.config import get_settings

pytestmark = pytest.mark.integration


def _plain_url(async_url: str) -> str:
    return async_url.replace("+asyncpg", "")


def _swap_db_name(url: str, db_name: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, "/" + db_name, parts.query, parts.fragment))


async def _connect(url: str) -> asyncpg.Connection:
    return await asyncpg.connect(_plain_url(url))


def _alembic(db_url: str, *args: str) -> None:
    env = {**os.environ, "DATABASE_URL": db_url}
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        env=env,
        capture_output=True,
        text=True,
        cwd=os.getcwd(),
        timeout=180,
        check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"alembic {' '.join(args)} failed (rc={proc.returncode}):\n"
            f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
        )


async def _table_exists(url: str, table: str) -> bool:
    conn = await _connect(url)
    try:
        row = await conn.fetchval("SELECT to_regclass($1)", f"public.{table}")
        return row is not None
    finally:
        await conn.close()


async def _column_exists(url: str, table: str, column: str) -> bool:
    conn = await _connect(url)
    try:
        row = await conn.fetchval(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = $1 AND column_name = $2",
            table,
            column,
        )
        return row is not None
    finally:
        await conn.close()


async def _seed_users_at_018(url: str) -> dict[str, int]:
    """Insert a super_admin, a leader+group, and two members BEFORE 019 runs.

    Returns the relevant ids so the post-backfill assertions can target them.
    Mirrors the real role/group invariants: super_admin -> group_id NULL,
    leader/member -> group_id NOT NULL.
    """
    conn = await _connect(url)
    try:
        sa_id = await conn.fetchval(
            "INSERT INTO users (username, role, group_id, password_reset_required, "
            " created_at, updated_at) "
            "VALUES ('sa', 'super_admin', NULL, false, now(), now()) RETURNING id"
        )
        leader_id = await conn.fetchval(
            "INSERT INTO users (username, role, group_id, password_reset_required, "
            " created_at, updated_at) "
            "VALUES ('lead', 'group_leader', NULL, false, now(), now()) RETURNING id"
        )
        group_id = await conn.fetchval(
            "INSERT INTO groups (name, leader_user_id, created_at) "
            "VALUES ('Team A', $1, now()) RETURNING id",
            leader_id,
        )
        # Promote leader's home group_id now that the group exists.
        await conn.execute("UPDATE users SET group_id = $1 WHERE id = $2", group_id, leader_id)
        m1 = await conn.fetchval(
            "INSERT INTO users (username, role, group_id, password_reset_required, "
            " created_at, updated_at) "
            "VALUES ('m1', 'group_member', $1, false, now(), now()) RETURNING id",
            group_id,
        )
        m2 = await conn.fetchval(
            "INSERT INTO users (username, role, group_id, password_reset_required, "
            " created_at, updated_at) "
            "VALUES ('m2', 'group_member', $1, false, now(), now()) RETURNING id",
            group_id,
        )
        return {
            "sa": int(sa_id),
            "leader": int(leader_id),
            "group": int(group_id),
            "m1": int(m1),
            "m2": int(m2),
        }
    finally:
        await conn.close()


def test_migration_019_backfill_and_downgrade_round_trip() -> None:
    from tests.conftest import _pg_available

    if not _pg_available():
        pytest.skip("postgres not reachable")

    base_url = get_settings().DATABASE_URL
    tmp_db = f"mas_mig019_{uuid.uuid4().hex[:12]}"
    tmp_url = _swap_db_name(base_url, tmp_db)

    async def _create() -> None:
        conn = await _connect(base_url)
        try:
            await conn.execute(f'CREATE DATABASE "{tmp_db}"')
        finally:
            await conn.close()

    async def _drop() -> None:
        conn = await _connect(base_url)
        try:
            await conn.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = $1 AND pid <> pg_backend_pid()",
                tmp_db,
            )
            await conn.execute(f'DROP DATABASE IF EXISTS "{tmp_db}"')
        finally:
            await conn.close()

    asyncio.run(_create())
    try:
        # 1. Bring the throwaway DB to the pre-019 head and seed users.
        _alembic(tmp_url, "upgrade", "20260527_018")
        assert not asyncio.run(_table_exists(tmp_url, "user_groups"))
        ids = asyncio.run(_seed_users_at_018(tmp_url))

        # 2. Apply 019.
        _alembic(tmp_url, "upgrade", "20260623_019")
        assert asyncio.run(_table_exists(tmp_url, "user_groups"))

        async def _assert_backfill() -> None:
            conn = await _connect(tmp_url)
            try:
                # Every group_id-bearing user has exactly one membership row
                # mirroring their home team.
                for key in ("leader", "m1", "m2"):
                    rows = await conn.fetch(
                        "SELECT group_id FROM user_groups WHERE user_id = $1", ids[key]
                    )
                    assert len(rows) == 1, f"{key} should have exactly 1 home membership"
                    assert int(rows[0]["group_id"]) == ids["group"]

                # super_admin gets NO row.
                sa_rows = await conn.fetch(
                    "SELECT 1 FROM user_groups WHERE user_id = $1", ids["sa"]
                )
                assert sa_rows == [], "super_admin must not be backfilled into user_groups"

                # Total = number of non-super_admin users.
                total = await conn.fetchval("SELECT count(*) FROM user_groups")
                assert int(total) == 3

                # UNIQUE(user_id, group_id) is enforced.
                with pytest.raises(asyncpg.UniqueViolationError):
                    await conn.execute(
                        "INSERT INTO user_groups (user_id, group_id) VALUES ($1, $2)",
                        ids["m1"],
                        ids["group"],
                    )

                # FK ON DELETE CASCADE: deleting a member drops their membership.
                await conn.execute("DELETE FROM users WHERE id = $1", ids["m2"])
                left = await conn.fetch("SELECT 1 FROM user_groups WHERE user_id = $1", ids["m2"])
                assert left == [], "membership must cascade-delete with the user"
            finally:
                await conn.close()

        asyncio.run(_assert_backfill())

        # 3. Downgrade drops the table but keeps users.group_id intact.
        async def _home_before() -> dict[int, int]:
            conn = await _connect(tmp_url)
            try:
                rows = await conn.fetch("SELECT id, group_id FROM users WHERE group_id IS NOT NULL")
                return {int(r["id"]): int(r["group_id"]) for r in rows}
            finally:
                await conn.close()

        home_map = asyncio.run(_home_before())
        _alembic(tmp_url, "downgrade", "20260527_018")
        assert not asyncio.run(_table_exists(tmp_url, "user_groups"))
        assert asyncio.run(_column_exists(tmp_url, "users", "group_id"))

        async def _home_after() -> dict[int, int]:
            conn = await _connect(tmp_url)
            try:
                rows = await conn.fetch("SELECT id, group_id FROM users WHERE group_id IS NOT NULL")
                return {int(r["id"]): int(r["group_id"]) for r in rows}
            finally:
                await conn.close()

        assert asyncio.run(_home_after()) == home_map, "users.group_id must survive downgrade"

        # 4. Replay 019 (idempotent backfill via ON CONFLICT DO NOTHING).
        _alembic(tmp_url, "upgrade", "20260623_019")
        assert asyncio.run(_table_exists(tmp_url, "user_groups"))
    finally:
        asyncio.run(_drop())
