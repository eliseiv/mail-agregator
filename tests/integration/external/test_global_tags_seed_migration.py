"""Global builtin-tag seed + migration ``20260709_023`` (ADR-0040 §1/§3).

- ``seed_builtin_tags`` seeds the GLOBAL builtin catalogue idempotently (second
  boot creates 0) and is race-safe under a concurrent boot (the partial-unique
  ``uq_tags_global_name`` absorbs the loser's IntegrityError in its SAVEPOINT);
- migration ``20260709_023`` makes ``tags.user_id`` nullable and adds the
  partial-unique index ``uq_tags_global_name ON tags(name) WHERE user_id IS
  NULL`` — so two GLOBAL tags cannot share a name, while a global and a personal
  tag may (the ``NULL`` owner is distinct under the composite unique).

State-based (like ``tests/unit/adr0038/test_migration_022``): the head schema is
inspected + exercised functionally. A destructive up/down cycle is NOT run
against the shared test DB (it would drop the index other tests depend on).
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from backend.app.tags.builtin import BUILTIN_TAGS
from backend.app.tags.service import seed_builtin_tags
from shared.models import Tag

pytestmark = pytest.mark.integration


async def _seed(db_engine: AsyncEngine) -> int:
    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with factory() as ses, ses.begin():
        return await seed_builtin_tags(ses)


class TestSeedBuiltinTags:
    async def test_idempotent_second_boot_creates_zero(self, db_engine: AsyncEngine) -> None:
        first = await _seed(db_engine)
        second = await _seed(db_engine)
        assert first == len(BUILTIN_TAGS), "first boot seeds the whole catalogue"
        assert second == 0, "second boot is a no-op"

    async def test_all_seeded_tags_are_global_and_builtin(self, db_engine: AsyncEngine) -> None:
        await _seed(db_engine)
        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as ses:
            rows = (
                await ses.execute(
                    text("SELECT count(*) FROM tags " "WHERE user_id IS NULL AND is_builtin = true")
                )
            ).scalar_one()
        assert rows == len(BUILTIN_TAGS)

    async def test_concurrent_seed_is_race_safe(self, db_engine: AsyncEngine) -> None:
        """Two concurrent boots: no exception escapes, and exactly one global
        row exists per builtin name (the partial-unique absorbs the race)."""
        results = await asyncio.gather(_seed(db_engine), _seed(db_engine), return_exceptions=True)
        assert all(not isinstance(r, BaseException) for r in results), results
        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as ses:
            n = (
                await ses.execute(
                    text("SELECT count(*) FROM tags WHERE user_id IS NULL AND is_builtin = true")
                )
            ).scalar_one()
        assert n == len(BUILTIN_TAGS), "no duplicate global builtin rows after the race"


class TestMigration023:
    async def test_head_includes_023(self, db_engine: AsyncEngine) -> None:
        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as ses:
            versions = {
                r[0]
                for r in (await ses.execute(text("SELECT version_num FROM alembic_version"))).all()
            }
        assert "20260709_023" in versions, versions

    async def test_user_id_is_nullable(self, db_engine: AsyncEngine) -> None:
        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as ses:
            is_nullable = (
                await ses.execute(
                    text(
                        "SELECT is_nullable FROM information_schema.columns "
                        "WHERE table_name = 'tags' AND column_name = 'user_id'"
                    )
                )
            ).scalar_one()
        assert is_nullable == "YES"

    async def test_partial_unique_index_exists(self, db_engine: AsyncEngine) -> None:
        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as ses:
            row = (
                await ses.execute(
                    text(
                        "SELECT indexdef FROM pg_indexes "
                        "WHERE tablename = 'tags' AND indexname = 'uq_tags_global_name'"
                    )
                )
            ).scalar_one_or_none()
        assert row is not None, "uq_tags_global_name index missing"
        assert "user_id IS NULL" in row, row

    async def test_two_global_tags_same_name_conflict(self, db_engine: AsyncEngine) -> None:
        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as ses, ses.begin():
            ses.add(Tag(user_id=None, name="dup-global", color="#2563eb"))
            await ses.flush()
        with pytest.raises(IntegrityError):
            factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
            async with factory() as ses, ses.begin():
                ses.add(Tag(user_id=None, name="dup-global", color="#dc2626"))
                await ses.flush()

    async def test_global_and_personal_same_name_allowed(self, db_engine: AsyncEngine) -> None:
        """The partial index only constrains ``user_id IS NULL`` rows, so a global
        and a personal tag may share a name (distinct under (user_id, name))."""
        # A real user is needed for the personal FK.
        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as ses, ses.begin():
            uid = (
                await ses.execute(
                    text(
                        "INSERT INTO users (username, display_name, role, "
                        "password_reset_required) "
                        "VALUES ('coexist_u', 'coexist', 'group_member', true) RETURNING id"
                    )
                )
            ).scalar_one()
            ses.add(Tag(user_id=None, name="coexist-name", color="#2563eb"))
            ses.add(Tag(user_id=int(uid), name="coexist-name", color="#dc2626"))
            await ses.flush()  # must NOT raise
