"""Integration tests for ``AuthService`` — direct service calls.

Targets the error / edge paths that ``test_auth_flow.py`` doesn't exercise:
- lockout edge cases (counter increments, lockout-window flip).
- set_password with bad / expired setup_token.
- complete_set_password creates a real session.
- is_currently_locked helper boundary behaviour.

Source of truth: ``backend/app/auth/service.py`` +
``docs/05-modules.md`` sec. 7.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from argon2 import PasswordHasher
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.auth.service import AuthService, seed_super_admin
from backend.app.exceptions import NotAuthenticatedError
from backend.app.repositories.users import UsersRepo

pytestmark = pytest.mark.integration


_PH = PasswordHasher()


@pytest_asyncio.fixture
async def fresh_user(db_session: AsyncSession) -> int:
    """Create a normal user with known password and return its id."""
    repo = UsersRepo(db_session)
    user = await repo.create(
        username="alice",
        email="alice@example.com",
        is_admin=False,
        password_hash=_PH.hash("correct-password"),
        password_reset_required=False,
    )
    await db_session.commit()
    return user.id


# ---------------------------------------------------------------------------
# is_currently_locked helper
# ---------------------------------------------------------------------------


class TestIsCurrentlyLocked:
    def test_none_returns_none(self) -> None:
        assert AuthService.is_currently_locked(None) is None

    def test_past_lockout_returns_none(self) -> None:
        past = datetime.now(UTC) - timedelta(minutes=10)
        assert AuthService.is_currently_locked(past) is None

    def test_future_lockout_returns_seconds(self) -> None:
        future = datetime.now(UTC) + timedelta(seconds=30)
        result = AuthService.is_currently_locked(future)
        assert result is not None
        assert 1 <= result <= 30

    def test_minimum_one_second(self) -> None:
        # Even when there's <1s left, we still report >=1.
        almost_now = datetime.now(UTC) + timedelta(milliseconds=100)
        result = AuthService.is_currently_locked(almost_now)
        assert result is not None
        assert result >= 1


# ---------------------------------------------------------------------------
# Login error paths
# ---------------------------------------------------------------------------


class TestLoginErrorPaths:
    async def test_unknown_user_returns_invalid(self, db_session: AsyncSession) -> None:
        svc = AuthService(db_session)
        async with db_session.begin():
            result = await svc.login(
                username="nobody",
                password="anything",
                ip="127.0.0.1",
                user_agent="test",
            )
        assert result.kind == "invalid"
        assert result.session_token is None

    async def test_wrong_password_returns_invalid(
        self, db_session: AsyncSession, fresh_user: int
    ) -> None:
        svc = AuthService(db_session)
        async with db_session.begin():
            result = await svc.login(
                username="alice",
                password="wrong-password",
                ip="127.0.0.1",
                user_agent="test",
            )
        assert result.kind == "invalid"

    async def test_correct_password_creates_session(
        self, db_session: AsyncSession, fresh_user: int
    ) -> None:
        svc = AuthService(db_session)
        async with db_session.begin():
            result = await svc.login(
                username="alice",
                password="correct-password",
                ip="127.0.0.1",
                user_agent="test",
            )
        assert result.kind == "session_created"
        assert result.session_token is not None
        assert result.csrf is not None
        assert result.role == "user"
        assert result.is_admin is False


# ---------------------------------------------------------------------------
# Lockout
# ---------------------------------------------------------------------------


class TestLockout:
    async def test_failed_attempts_increment(
        self, db_session: AsyncSession, fresh_user: int
    ) -> None:
        """Each failed login bumps ``failed_login_attempts`` until it
        reaches the threshold; on threshold a ``lockout_until`` is set.
        """
        from shared.config import get_settings

        s = get_settings()
        threshold = s.LOGIN_FAILURE_THRESHOLD

        svc = AuthService(db_session)
        # Submit threshold wrong passwords: each marked invalid; on the last
        # one the lockout fires.
        for _ in range(threshold):
            async with db_session.begin():
                r = await svc.login(
                    username="alice",
                    password="bad",
                    ip="127.0.0.1",
                    user_agent="t",
                )
            assert r.kind == "invalid"

        # Roll back the implicit autobegin transaction created by the
        # check below, so we can verify state via a fresh autobegin.
        await db_session.rollback()
        db_session.expire_all()
        user = await UsersRepo(db_session).get_by_id(fresh_user)
        assert user is not None
        assert user.failed_login_attempts == threshold
        assert user.lockout_until is not None
        assert user.lockout_until > datetime.now(UTC)

    async def test_locked_account_rejects_even_correct_password(
        self, db_session: AsyncSession, fresh_user: int
    ) -> None:
        # Manually set lockout_until in the future.
        UsersRepo(db_session)
        future = datetime.now(UTC) + timedelta(minutes=5)
        async with db_session.begin():
            from sqlalchemy import update

            from shared.models import User

            await db_session.execute(
                update(User).where(User.id == fresh_user).values(lockout_until=future)
            )

        svc = AuthService(db_session)
        async with db_session.begin():
            result = await svc.login(
                username="alice",
                password="correct-password",
                ip="127.0.0.1",
                user_agent="t",
            )
        assert result.kind == "locked"
        assert result.retry_after_sec is not None
        assert result.retry_after_sec >= 1

    async def test_expired_lockout_lets_login_proceed(
        self, db_session: AsyncSession, fresh_user: int
    ) -> None:
        # lockout_until in the past -> should not block.
        UsersRepo(db_session)
        past = datetime.now(UTC) - timedelta(minutes=5)
        async with db_session.begin():
            from sqlalchemy import update

            from shared.models import User

            await db_session.execute(
                update(User).where(User.id == fresh_user).values(lockout_until=past)
            )

        svc = AuthService(db_session)
        async with db_session.begin():
            result = await svc.login(
                username="alice",
                password="correct-password",
                ip="127.0.0.1",
                user_agent="t",
            )
        assert result.kind == "session_created"


# ---------------------------------------------------------------------------
# Set-password flow
# ---------------------------------------------------------------------------


class TestSetPassword:
    async def test_invalid_setup_token_raises_not_authenticated(
        self, db_session: AsyncSession
    ) -> None:
        svc = AuthService(db_session)
        with pytest.raises(NotAuthenticatedError):
            async with db_session.begin():
                await svc.complete_set_password(
                    setup_token="bogus-not-a-real-token",
                    password="N3wPassword!",
                    ip="127.0.0.1",
                    user_agent="t",
                )

    async def test_password_reset_required_path_returns_setup_session(
        self, db_session: AsyncSession
    ) -> None:
        # User with password_reset_required=True - login must return
        # set_password_required regardless of submitted password.
        repo = UsersRepo(db_session)
        await repo.create(
            username="newcomer",
            email=None,
            is_admin=False,
            password_hash=None,
            password_reset_required=True,
        )
        await db_session.commit()

        svc = AuthService(db_session)
        async with db_session.begin():
            result = await svc.login(
                username="newcomer",
                password="anything-at-all",
                ip="127.0.0.1",
                user_agent="t",
            )
        assert result.kind == "set_password_required"
        assert result.setup_token is not None
        assert result.csrf is not None

    async def test_set_password_completes_and_creates_session(
        self, db_session: AsyncSession
    ) -> None:
        repo = UsersRepo(db_session)
        await repo.create(
            username="newcomer2",
            email=None,
            is_admin=False,
            password_hash=None,
            password_reset_required=True,
        )
        await db_session.commit()

        svc = AuthService(db_session)
        async with db_session.begin():
            login = await svc.login(
                username="newcomer2",
                password="ignored",
                ip="127.0.0.1",
                user_agent="t",
            )
        assert login.setup_token is not None

        async with db_session.begin():
            session_result = await svc.complete_set_password(
                setup_token=login.setup_token,
                password="N3wPassword!Strong",
                ip="127.0.0.1",
                user_agent="t",
            )
        assert session_result.kind == "session_created"
        assert session_result.session_token is not None
        # Setup token is now revoked — re-using must fail.
        with pytest.raises(NotAuthenticatedError):
            async with db_session.begin():
                await svc.complete_set_password(
                    setup_token=login.setup_token,
                    password="another",
                    ip="127.0.0.1",
                    user_agent="t",
                )


# ---------------------------------------------------------------------------
# Logout
# ---------------------------------------------------------------------------


class TestLogout:
    async def test_logout_revokes_session(self, db_session: AsyncSession, fresh_user: int) -> None:
        from backend.app.sessions import SessionStore

        svc = AuthService(db_session)
        async with db_session.begin():
            result = await svc.login(
                username="alice",
                password="correct-password",
                ip="127.0.0.1",
                user_agent="t",
            )
        assert result.session_token is not None
        token = result.session_token

        # Verify session exists.
        store = SessionStore()
        sess = await store.get(token)
        assert sess is not None

        await svc.logout(
            session_token=token,
            actor_user_id=sess.user_id,
            is_admin=False,
            ip="127.0.0.1",
            user_agent="t",
        )

        # Now revoked.
        sess_after = await store.get(token)
        assert sess_after is None


# ---------------------------------------------------------------------------
# seed_super_admin idempotency
# ---------------------------------------------------------------------------


class TestSeedSuperAdmin:
    async def test_seed_then_reseed_is_unchanged(self, db_session: AsyncSession) -> None:
        # First seed creates.
        async with db_session.begin():
            status1 = await seed_super_admin(db_session)
        assert status1 in {"created", "updated", "unchanged"}
        # Second seed with the same credentials must be either updated or
        # unchanged (never re-created).
        async with db_session.begin():
            status2 = await seed_super_admin(db_session)
        assert status2 in {"updated", "unchanged"}
