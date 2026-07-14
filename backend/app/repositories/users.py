"""User repository (ADR-0044: technical mailbox-owner table).

After the decommission (ADR-0043 §4) ``users`` carries only the ``crm-service``
technical row: no interactive users, roles or groups. What survives is what the
kept paths need:

- ``get_by_id`` / ``get_by_username`` — mailbox-owner resolution (external
  write, ``accounts/service.py``, ``oauth/service.py``);
- ``get_many_by_ids`` — bulk owner resolution in ``accounts/service.py``;
- ``create`` — the ``crm-service`` seed (``auth/service.py``).

Removed with the UI / groups / audit: ``get_admin`` (the worker audit writer),
``list_paged`` / ``list_in_group`` / ``list_user_ids_in_group`` (admin UI,
``users.group_id``), ``upsert_admin`` (``seed_super_admin``) and the
login/lockout bookkeeping (``set_password_hash`` / ``reset_password`` /
``record_login_*``).
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models import ROLE_GROUP_MEMBER, User


class UsersRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    # --- Reads -------------------------------------------------------------

    async def get_by_id(self, user_id: int) -> User | None:
        return await self._s.get(User, user_id)

    async def get_by_username(self, username: str) -> User | None:
        stmt = select(User).where(User.username == username.lower())
        return (await self._s.execute(stmt)).scalar_one_or_none()

    async def get_many_by_ids(self, ids: list[int]) -> dict[int, User]:
        """Bulk-load users by id; missing ids are simply absent from the dict."""
        if not ids:
            return {}
        stmt = select(User).where(User.id.in_(ids))
        out: dict[int, User] = {}
        for user in (await self._s.execute(stmt)).scalars():
            out[user.id] = user
        return out

    # --- Writes ------------------------------------------------------------

    async def create(
        self,
        *,
        username: str,
        email: str | None,
        role: str = ROLE_GROUP_MEMBER,
        display_name: str | None = None,
        password_hash: str | None = None,
        password_reset_required: bool = True,
        password_encrypted: bytes | None = None,
    ) -> User:
        """Insert a new user. Raises :class:`IntegrityError` on username clash.

        The only caller after the decommission is ``seed_crm_service_user``
        (``role='super_admin'``, no password).
        """
        user = User(
            username=username.lower(),
            email=email,
            display_name=display_name,
            role=role,
            password_hash=password_hash,
            password_reset_required=password_reset_required,
            password_encrypted=password_encrypted,
        )
        self._s.add(user)
        try:
            await self._s.flush()
        except IntegrityError:
            raise
        await self._s.refresh(user)
        return user

    async def update_fields(self, user_id: int, **fields: object) -> None:
        if not fields:
            return
        fields["updated_at"] = datetime.now(UTC)
        await self._s.execute(update(User).where(User.id == user_id).values(**fields))

    async def delete(self, user_id: int) -> None:
        stmt = text("DELETE FROM users WHERE id = :id")
        await self._s.execute(stmt, {"id": user_id})
