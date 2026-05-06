"""User repository: CRUD + lockout / failed-attempts bookkeeping."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import case, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models import User


class UsersRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    # --- Reads -------------------------------------------------------------

    async def get_by_id(self, user_id: int) -> User | None:
        return await self._s.get(User, user_id)

    async def get_by_username(self, username: str) -> User | None:
        stmt = select(User).where(User.username == username.lower())
        return (await self._s.execute(stmt)).scalar_one_or_none()

    async def get_admin(self) -> User | None:
        stmt = select(User).where(User.is_admin.is_(True)).limit(1)
        return (await self._s.execute(stmt)).scalar_one_or_none()

    async def list_paged(self, q: str | None, page: int, limit: int) -> tuple[list[User], int]:
        stmt = select(User).order_by(User.id)
        count_stmt = select(func.count()).select_from(User)
        if q:
            pattern = f"%{q.lower()}%"
            stmt = stmt.where(
                or_(
                    func.lower(User.username).like(pattern),
                    func.lower(func.coalesce(User.email, "")).like(pattern),
                )
            )
            count_stmt = count_stmt.where(
                or_(
                    func.lower(User.username).like(pattern),
                    func.lower(func.coalesce(User.email, "")).like(pattern),
                )
            )
        total = (await self._s.execute(count_stmt)).scalar_one()
        page = max(page, 1)
        limit = max(min(limit, 200), 1)
        stmt = stmt.offset((page - 1) * limit).limit(limit)
        items = list((await self._s.execute(stmt)).scalars().all())
        return items, int(total)

    # --- Writes ------------------------------------------------------------

    async def create(
        self,
        *,
        username: str,
        email: str | None,
        is_admin: bool = False,
        password_hash: str | None = None,
        password_reset_required: bool = True,
    ) -> User:
        """Insert a new user. Raises :class:`IntegrityError` on username clash."""
        user = User(
            username=username.lower(),
            email=email,
            password_hash=password_hash,
            is_admin=is_admin,
            password_reset_required=password_reset_required,
        )
        self._s.add(user)
        try:
            await self._s.flush()
        except IntegrityError:
            raise
        await self._s.refresh(user)
        return user

    async def upsert_admin(self, *, username: str, password_hash: str) -> tuple[User, str]:
        """Idempotent super-admin seed.

        Returns the row plus a status string: ``created`` / ``updated`` /
        ``unchanged``. Used by ``seed_super_admin`` in :mod:`backend.app.auth.service`.
        """
        username = username.lower()
        existing = await self.get_by_username(username)
        if existing is None:
            stmt = (
                pg_insert(User)
                .values(
                    username=username,
                    password_hash=password_hash,
                    is_admin=True,
                    password_reset_required=False,
                    failed_login_attempts=0,
                    lockout_until=None,
                )
                .returning(User)
            )
            row = (await self._s.execute(stmt)).scalar_one()
            return row, "created"

        # Update — sync password & reset lockout state every boot.
        existing.password_hash = password_hash
        existing.is_admin = True
        existing.password_reset_required = False
        existing.failed_login_attempts = 0
        existing.lockout_until = None
        existing.updated_at = datetime.now(UTC)
        await self._s.flush()
        return existing, "updated"

    async def set_password_hash(self, user_id: int, password_hash: str) -> None:
        stmt = (
            update(User)
            .where(User.id == user_id)
            .values(
                password_hash=password_hash,
                password_reset_required=False,
                failed_login_attempts=0,
                lockout_until=None,
                updated_at=datetime.now(UTC),
            )
        )
        await self._s.execute(stmt)

    async def reset_password(self, user_id: int) -> None:
        stmt = (
            update(User)
            .where(User.id == user_id)
            .values(
                password_hash=None,
                password_reset_required=True,
                failed_login_attempts=0,
                lockout_until=None,
                updated_at=datetime.now(UTC),
            )
        )
        await self._s.execute(stmt)

    async def delete(self, user_id: int) -> None:
        stmt = text("DELETE FROM users WHERE id = :id")
        await self._s.execute(stmt, {"id": user_id})

    async def record_login_success(self, user_id: int) -> None:
        stmt = (
            update(User)
            .where(User.id == user_id)
            .values(
                failed_login_attempts=0,
                lockout_until=None,
                last_login_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )
        await self._s.execute(stmt)

    async def record_login_failure(
        self, user_id: int, *, threshold: int, lockout_minutes: int
    ) -> tuple[int, datetime | None]:
        """Increment ``failed_login_attempts``; set ``lockout_until`` if over.

        Returns ``(new_attempts, lockout_until)``.
        """
        # Atomic increment with conditional lockout via expressions.
        now = datetime.now(UTC)
        lockout_at = now + timedelta(minutes=lockout_minutes)
        stmt = (
            update(User)
            .where(User.id == user_id)
            .values(
                failed_login_attempts=User.failed_login_attempts + 1,
                # Top-level ``case(...)`` is the SQL CASE expression; ``func.case``
                # would call a SQL function literally named ``case`` which doesn't
                # accept ``else_`` and raises TypeError at execute-time. Bug fix:
                # without this lockout was never set on brute-force attempts.
                lockout_until=case(
                    (
                        User.failed_login_attempts + 1 >= threshold,
                        lockout_at,
                    ),
                    else_=User.lockout_until,
                ),
                updated_at=now,
            )
            .returning(User.failed_login_attempts, User.lockout_until)
        )
        row = (await self._s.execute(stmt)).one()
        return int(row[0]), row[1]
