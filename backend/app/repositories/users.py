"""User repository: CRUD + lockout / failed-attempts bookkeeping.

Post-ADR-0019: ``is_admin`` is replaced with ``role`` and users gain a
nullable ``group_id``. A ``display_name`` column is also stored. See
``docs/03-data-model.md`` table ``users``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import case, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models import ROLE_GROUP_LEADER, ROLE_GROUP_MEMBER, ROLE_SUPER_ADMIN, User


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
        """Return the (single) super-admin row, or ``None``."""
        stmt = select(User).where(User.role == ROLE_SUPER_ADMIN).limit(1)
        return (await self._s.execute(stmt)).scalar_one_or_none()

    async def list_paged(
        self,
        q: str | None,
        page: int,
        limit: int,
        *,
        group_id: int | None = None,
        role: str | None = None,
        in_group_ids: list[int] | None = None,
    ) -> tuple[list[User], int]:
        """List users with pagination and optional filters.

        Filters:

        - ``q`` — substring on ``username`` / ``email``.
        - ``group_id`` — exact match on ``users.group_id``.
        - ``role`` — exact match on ``users.role``.
        - ``in_group_ids`` — restrict to users whose ``group_id`` is in
          this list (used by group leaders to scope their visibility).
        """
        # Admin list ordering (FE-FIX round-3 #2):
        #   1) ungrouped users first   (group_id IS NULL — NULLS FIRST)
        #   2) within a group, leader before members
        #   3) stable tiebreak by id
        role_rank = case(
            (User.role == ROLE_GROUP_LEADER, 0),
            else_=1,
        )
        stmt = select(User).order_by(
            User.group_id.asc().nullsfirst(),
            role_rank,
            User.id,
        )
        count_stmt = select(func.count()).select_from(User)
        if q:
            pattern = f"%{q.lower()}%"
            cond = or_(
                func.lower(User.username).like(pattern),
                func.lower(func.coalesce(User.email, "")).like(pattern),
            )
            stmt = stmt.where(cond)
            count_stmt = count_stmt.where(cond)
        if group_id is not None:
            stmt = stmt.where(User.group_id == group_id)
            count_stmt = count_stmt.where(User.group_id == group_id)
        if role is not None:
            stmt = stmt.where(User.role == role)
            count_stmt = count_stmt.where(User.role == role)
        if in_group_ids is not None:
            if not in_group_ids:
                # Empty restriction == nothing visible.
                return [], 0
            stmt = stmt.where(User.group_id.in_(in_group_ids))
            count_stmt = count_stmt.where(User.group_id.in_(in_group_ids))
        total = (await self._s.execute(count_stmt)).scalar_one()
        page = max(page, 1)
        limit = max(min(limit, 200), 1)
        stmt = stmt.offset((page - 1) * limit).limit(limit)
        items = list((await self._s.execute(stmt)).scalars().all())
        return items, int(total)

    async def get_many_by_ids(self, ids: list[int]) -> dict[int, User]:
        """Bulk-load users by id; missing ids are simply absent from the dict."""
        if not ids:
            return {}
        stmt = select(User).where(User.id.in_(ids))
        out: dict[int, User] = {}
        for user in (await self._s.execute(stmt)).scalars():
            out[user.id] = user
        return out

    async def list_user_ids_in_group(self, group_id: int) -> list[int]:
        """All ``users.id`` values whose ``group_id`` matches.

        Returns ``[]`` if no users belong to the group. Used by visibility
        scope helpers in :class:`backend.app.accounts.service.MailAccountService`
        and :class:`backend.app.messages.service.MessageService` to compute
        the set of mail-accounts the caller is allowed to see.
        """
        stmt = select(User.id).where(User.group_id == group_id)
        return [int(row[0]) for row in (await self._s.execute(stmt)).all()]

    async def list_in_group(self, group_id: int) -> list[User]:
        stmt = select(User).where(User.group_id == group_id).order_by(User.id)
        return list((await self._s.execute(stmt)).scalars().all())

    # --- Writes ------------------------------------------------------------

    async def create(
        self,
        *,
        username: str,
        email: str | None,
        role: str = ROLE_GROUP_MEMBER,
        group_id: int | None = None,
        display_name: str | None = None,
        password_hash: str | None = None,
        password_reset_required: bool = True,
    ) -> User:
        """Insert a new user. Raises :class:`IntegrityError` on username clash.

        Caller is responsible for satisfying the ``users_role_group_invariant``
        — pre-ADR-0019 callers (auth.seed_super_admin) pass ``role='super_admin'``
        with ``group_id=None``; admin.create_user passes a concrete group.
        For the auto-create-leader flow the caller wraps INSERT-user +
        INSERT-groups + UPDATE-user.group_id in a single transaction with the
        FK ``DEFERRABLE INITIALLY DEFERRED``, so this method may be called
        with ``role='group_leader'`` and ``group_id=None``; the trigger
        validates the invariant at COMMIT time.
        """
        user = User(
            username=username.lower(),
            email=email,
            display_name=display_name,
            role=role,
            group_id=group_id,
            password_hash=password_hash,
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
                    role=ROLE_SUPER_ADMIN,
                    group_id=None,
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
        existing.role = ROLE_SUPER_ADMIN
        existing.group_id = None
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

    async def update_fields(self, user_id: int, **fields: object) -> None:
        if not fields:
            return
        fields["updated_at"] = datetime.now(UTC)
        await self._s.execute(update(User).where(User.id == user_id).values(**fields))

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
