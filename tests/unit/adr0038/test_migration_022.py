"""ADR-0038 §1 — migration ``20260706_022`` applied on head.

Covered:
- the alembic head recorded in ``alembic_version`` is ``20260706_022``;
- ``users.password_encrypted`` exists as a nullable ``BYTEA`` column.

Source of truth: ``migrations/versions/20260706_022_user_password_encrypted.py``
+ ADR-0038 §1.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

pytestmark = pytest.mark.integration


class TestMigration022:
    async def test_head_revision_is_022(self, db_engine: AsyncEngine) -> None:
        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as ses:
            versions = {
                r[0]
                for r in (await ses.execute(text("SELECT version_num FROM alembic_version"))).all()
            }
        assert "20260706_022" in versions, versions

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
