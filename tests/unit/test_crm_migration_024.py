"""ADR-0043 §2 — migration ``20260710_024`` (``messages.pushed_at`` push-outbox marker).

Covered (robust against newly-added migrations — like ``test_migration_022``):
- DB ``alembic_version`` head == the current script head (single head, DB fully migrated;
  ``get_current_head`` raises on a branched history);
- ``20260710_024`` is a real ancestor of that head (applied, not skipped) and revises
  ``20260709_023`` (linear chain, single parent);
- ``messages.pushed_at`` is a nullable ``TIMESTAMPTZ``;
- the partial index ``ix_messages_pushed_at_pending`` (``WHERE pushed_at IS NULL``) exists.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

pytestmark = pytest.mark.integration  # needs DB migrated to head

# tests/unit/test_crm_migration_024.py -> parents[2] = repo root.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_REV_024 = "20260710_024"
_REV_023 = "20260709_023"


def _script_directory() -> ScriptDirectory:
    cfg = Config(str(_REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_REPO_ROOT / "migrations"))
    return ScriptDirectory.from_config(cfg)


async def _db_head(db_engine: AsyncEngine) -> set[str]:
    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with factory() as ses:
        return {
            r[0] for r in (await ses.execute(text("SELECT version_num FROM alembic_version"))).all()
        }


async def test_db_head_matches_single_script_head(db_engine: AsyncEngine) -> None:
    """DB is migrated to the SINGLE script head (get_current_head raises on a branch)."""
    versions = await _db_head(db_engine)
    script_head = _script_directory().get_current_head()  # raises if multiple heads
    assert versions == {script_head}, (versions, script_head)


async def test_024_in_ancestry_and_revises_023(db_engine: AsyncEngine) -> None:
    versions = await _db_head(db_engine)
    (head,) = tuple(versions)
    script = _script_directory()
    ancestry = {rev.revision for rev in script.iterate_revisions(head, "base")}
    assert _REV_024 in ancestry, ancestry
    # Linear chain: 024 revises exactly 023 (single parent).
    assert script.get_revision(_REV_024).down_revision == _REV_023


async def test_pushed_at_is_nullable_timestamptz(db_engine: AsyncEngine) -> None:
    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with factory() as ses:
        row = (
            await ses.execute(
                text(
                    "SELECT data_type, is_nullable FROM information_schema.columns "
                    "WHERE table_name = 'messages' AND column_name = 'pushed_at'"
                )
            )
        ).one_or_none()
    assert row is not None, "messages.pushed_at column missing"
    data_type, is_nullable = row
    assert data_type == "timestamp with time zone", data_type
    assert is_nullable == "YES", is_nullable


async def test_pending_partial_index_exists(db_engine: AsyncEngine) -> None:
    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with factory() as ses:
        row = (
            await ses.execute(
                text(
                    "SELECT indexdef FROM pg_indexes "
                    "WHERE tablename = 'messages' AND indexname = 'ix_messages_pushed_at_pending'"
                )
            )
        ).one_or_none()
    assert row is not None, "ix_messages_pushed_at_pending missing"
    indexdef = row[0].lower()
    assert "pushed_at is null" in indexdef, indexdef  # partial predicate preserved
