"""A (downgrade). Migration 018 up/down round-trip in an isolated DB.

We never touch the shared test DB (other tests assume it stays at head with
its tables intact). Instead this test:

1. ``CREATE DATABASE`` a throwaway DB on the same Postgres server (asyncpg —
   the only Postgres driver installed).
2. ``alembic upgrade 20260527_017`` then ``upgrade 20260527_018`` (subprocess,
   ``DATABASE_URL`` pointed at the temp DB) — proves 018 applies on top of 017.
3. ``alembic downgrade 20260527_017`` — proves the down-revision drops the
   oauth columns/constraints and restores ``encrypted_password NOT NULL``.
4. ``alembic upgrade 20260527_018`` again — proves the migration replays.
5. ``DROP DATABASE``.

The alembic ``env.py`` reads ``DATABASE_URL`` (via ``get_settings()``) rather
than the ini ``sqlalchemy.url``; ``get_settings`` is ``lru_cache``-d in-process,
so we drive alembic in a **subprocess** with an overridden ``DATABASE_URL`` to
target the throwaway DB without disturbing the in-process settings singleton.
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
    """asyncpg.connect wants a libpq URL with no ``+asyncpg`` suffix."""
    return async_url.replace("+asyncpg", "")


def _swap_db_name(url: str, db_name: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, "/" + db_name, parts.query, parts.fragment))


async def _connect(url: str) -> asyncpg.Connection:
    return await asyncpg.connect(_plain_url(url))


async def _columns(url: str, table: str) -> dict[str, str]:
    conn = await _connect(url)
    try:
        rows = await conn.fetch(
            "SELECT column_name, is_nullable FROM information_schema.columns "
            "WHERE table_name = $1",
            table,
        )
        return {r["column_name"]: r["is_nullable"] for r in rows}
    finally:
        await conn.close()


async def _check_constraint_names(url: str, table: str) -> set[str]:
    conn = await _connect(url)
    try:
        rows = await conn.fetch(
            "SELECT conname FROM pg_constraint " "WHERE conrelid = $1::regclass AND contype = 'c'",
            table,
        )
        return {r["conname"] for r in rows}
    finally:
        await conn.close()


def _alembic(db_url: str, *args: str) -> None:
    env = {**os.environ, "DATABASE_URL": db_url}
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        env=env,
        capture_output=True,
        text=True,
        cwd=os.getcwd(),
        timeout=120,
        check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"alembic {' '.join(args)} failed (rc={proc.returncode}):\n"
            f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
        )


def test_migration_018_up_down_up_round_trip() -> None:
    from tests.conftest import _pg_available

    if not _pg_available():
        pytest.skip("postgres not reachable")

    base_url = get_settings().DATABASE_URL
    tmp_db = f"mas_mig_test_{uuid.uuid4().hex[:12]}"
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
        # 2. upgrade 017 -> 018.
        _alembic(tmp_url, "upgrade", "20260527_017")
        cols_017 = asyncio.run(_columns(tmp_url, "mail_accounts"))
        assert "auth_type" not in cols_017
        assert cols_017["encrypted_password"] == "NO"  # NOT NULL pre-018

        _alembic(tmp_url, "upgrade", "20260527_018")
        cols_018 = asyncio.run(_columns(tmp_url, "mail_accounts"))
        assert "auth_type" in cols_018
        assert cols_018["encrypted_password"] == "YES"  # nullable after 018
        assert "oauth_refresh_token_encrypted" in cols_018
        assert "proxy_url" in cols_018
        names_018 = asyncio.run(_check_constraint_names(tmp_url, "mail_accounts"))
        assert {
            "ck_mail_accounts_auth_type",
            "ck_mail_accounts_password_creds",
            "ck_mail_accounts_oauth_creds",
        } <= names_018

        # 3. downgrade 018 -> 017.
        _alembic(tmp_url, "downgrade", "20260527_017")
        cols_back = asyncio.run(_columns(tmp_url, "mail_accounts"))
        assert "auth_type" not in cols_back
        assert "oauth_refresh_token_encrypted" not in cols_back
        assert "proxy_url" not in cols_back
        assert cols_back["encrypted_password"] == "NO"
        names_back = asyncio.run(_check_constraint_names(tmp_url, "mail_accounts"))
        assert "ck_mail_accounts_auth_type" not in names_back
        assert "ck_mail_accounts_oauth_creds" not in names_back

        # 4. re-upgrade 017 -> 018 (replayable).
        _alembic(tmp_url, "upgrade", "20260527_018")
        assert "auth_type" in asyncio.run(_columns(tmp_url, "mail_accounts"))
    finally:
        asyncio.run(_drop())
