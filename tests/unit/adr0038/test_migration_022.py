"""ADR-0038 §1 — migration ``20260706_022`` applied + its schema change live.

Covered:
- the DB's ``alembic_version`` head equals the CURRENT Alembic script head
  (robust: any newly-added migration, e.g. ADR-0040's ``20260709_023``, keeps
  this green — the invariant is "the DB is fully migrated", not a hard-coded
  revision id);
- ``20260706_022`` is in the applied ancestry of that head (ADR-0038 was really
  applied, not skipped);
- ``users.password_encrypted`` exists as a nullable ``BYTEA`` column.

Source of truth: ``migrations/versions/20260706_022_user_password_encrypted.py``
+ ADR-0038 §1.

round-S4-A: the previous ``test_head_revision_is_022`` hard-coded
``"20260706_022" in versions``. ``alembic_version`` stores ONLY the single
current head (linear history), so once ADR-0040's ``20260709_023`` moved the
head this literal broke (orphaned by a superseding, ADR-approved migration).
The assertion is now derived from the Alembic script directory (the single
source of truth for the head) and an explicit ancestry check for 022 — it can
never be orphaned again by simply adding a migration.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

pytestmark = pytest.mark.integration

# Repo root: tests/unit/adr0038/test_migration_022.py → parents[3].
_REPO_ROOT = Path(__file__).resolve().parents[3]


def _script_directory() -> ScriptDirectory:
    """Alembic ``ScriptDirectory`` bound to the repo's migrations (absolute paths
    so the test is independent of the pytest invocation cwd)."""
    cfg = Config(str(_REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_REPO_ROOT / "migrations"))
    return ScriptDirectory.from_config(cfg)


async def _db_head(db_engine: AsyncEngine) -> set[str]:
    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with factory() as ses:
        return {
            r[0] for r in (await ses.execute(text("SELECT version_num FROM alembic_version"))).all()
        }


class TestMigration022:
    async def test_db_head_matches_alembic_script_head(self, db_engine: AsyncEngine) -> None:
        """The DB is migrated to the CURRENT script head (whatever it is)."""
        versions = await _db_head(db_engine)
        script_head = _script_directory().get_current_head()
        assert versions == {script_head}, (versions, script_head)

    async def test_022_is_in_applied_ancestry(self, db_engine: AsyncEngine) -> None:
        """ADR-0038's ``20260706_022`` is a real ancestor of the current head —
        it was applied, not skipped (its schema change below is therefore live)."""
        versions = await _db_head(db_engine)
        (head,) = tuple(versions)
        script = _script_directory()
        ancestry = {rev.revision for rev in script.iterate_revisions(head, "base")}
        assert "20260706_022" in ancestry, ancestry

    async def test_password_encrypted_column_is_nullable_bytea(
        self, db_engine: AsyncEngine
    ) -> None:
        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as ses:
            row = (
                await ses.execute(
                    text(
                        "SELECT data_type, is_nullable FROM information_schema.columns "
                        "WHERE table_name = 'users' AND column_name = 'password_encrypted'"
                    )
                )
            ).one_or_none()
        assert row is not None, "users.password_encrypted column missing"
        data_type, is_nullable = row
        assert data_type == "bytea", data_type
        assert is_nullable == "YES", is_nullable
