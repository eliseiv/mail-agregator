"""Integration tests for the idempotent ``seed_super_admin`` UPSERT.

Source of truth: ``backend/app/auth/service.py::seed_super_admin``.
"""

from __future__ import annotations

import pytest
from argon2 import PasswordHasher
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.auth.service import seed_super_admin
from backend.app.repositories.users import UsersRepo

pytestmark = pytest.mark.integration


class TestSeed:
    async def test_first_run_creates(self, db_session: AsyncSession) -> None:
        async with db_session.begin():
            status = await seed_super_admin(db_session)
        assert status == "created"
        admin = await UsersRepo(db_session).get_by_username("admin")
        assert admin is not None
        assert admin.is_admin is True
        assert admin.password_reset_required is False
        assert admin.password_hash is not None

    async def test_second_run_unchanged_when_password_matches(
        self, db_session: AsyncSession
    ) -> None:
        async with db_session.begin():
            await seed_super_admin(db_session)
        # Same env var still in place — second call should be unchanged.
        async with db_session.begin():
            status = await seed_super_admin(db_session)
        assert status == "unchanged"

    async def test_password_change_triggers_update(self, db_session: AsyncSession) -> None:
        # First seed + commit so we can start a fresh transaction.
        async with db_session.begin():
            await seed_super_admin(db_session)
        original = await UsersRepo(db_session).get_by_username("admin")
        assert original is not None
        original_hash = original.password_hash
        # Close out the autobegun read-tx so the next ``begin()`` is allowed.
        await db_session.rollback()

        # Rotate the env password and re-seed.
        from shared.config import get_settings

        s = get_settings()
        object.__setattr__(s, "ADMIN_PASSWORD", "rotated-password-xyz")
        try:
            async with db_session.begin():
                status = await seed_super_admin(db_session)
            assert status == "updated"

            updated = await UsersRepo(db_session).get_by_username("admin")
            assert updated is not None
            ph = PasswordHasher()
            assert ph.verify(updated.password_hash, "rotated-password-xyz") is True
            assert updated.password_hash != original_hash
        finally:
            # Restore so other tests still log in with the original password.
            object.__setattr__(s, "ADMIN_PASSWORD", "qa_admin_password_for_tests_long_enough")
