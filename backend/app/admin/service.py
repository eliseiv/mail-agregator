"""AdminService — list/create/reset/delete users + read audit log."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.admin.schemas import (
    AuditEntryDTO,
    AuditListResponse,
    CreateUserRequest,
    CreateUserResponse,
    DeleteUserResponse,
    UserDTO,
    UserMailAccountSummary,
    UsersListResponse,
)
from backend.app.audit import AuditWriter
from backend.app.exceptions import (
    CannotDeleteAdminError,
    CannotResetAdminError,
    ConflictError,
    NotFoundError,
)
from backend.app.repositories.audit import AuditRepo
from backend.app.repositories.mail_accounts import MailAccountsRepo
from backend.app.repositories.messages import MessagesRepo
from backend.app.repositories.users import UsersRepo
from backend.app.sessions import SessionStore
from shared.logging import get_logger
from shared.storage import get_storage

log = get_logger(__name__)


class AdminService:
    def __init__(self, session: AsyncSession) -> None:
        self._db = session
        self._users = UsersRepo(session)
        self._accounts = MailAccountsRepo(session)
        self._messages = MessagesRepo(session)
        self._audit_repo = AuditRepo(session)
        self._audit = AuditWriter(session)
        self._sessions = SessionStore()
        self._storage = get_storage()

    # --- Users -------------------------------------------------------------

    async def list_users(self, *, q: str | None, page: int, limit: int) -> UsersListResponse:
        users, total = await self._users.list_paged(q, page, limit)
        accs_map = await self._accounts.list_for_users([u.id for u in users])
        items = [
            UserDTO(
                id=u.id,
                username=u.username,
                email=u.email,
                is_admin=u.is_admin,
                password_reset_required=u.password_reset_required,
                lockout_until=u.lockout_until,
                last_login_at=u.last_login_at,
                created_at=u.created_at,
                mail_accounts=[
                    UserMailAccountSummary(
                        id=a.id,
                        email=a.email,
                        is_active=a.is_active,
                        last_synced_at=a.last_synced_at,
                        last_sync_error=a.last_sync_error,
                    )
                    for a in accs_map.get(u.id, [])
                ],
            )
            for u in users
        ]
        return UsersListResponse(items=items, total=total, page=page, limit=limit)

    async def create_user(
        self,
        *,
        payload: CreateUserRequest,
        actor_id: int,
        ip: str,
        user_agent: str | None,
    ) -> CreateUserResponse:
        try:
            user = await self._users.create(
                username=payload.username,
                email=payload.email,
                is_admin=False,
                password_hash=None,
                password_reset_required=True,
            )
        except IntegrityError as exc:
            raise ConflictError("Username already exists", field="username") from exc

        await self._audit.log(
            actor_user_id=actor_id,
            action="create_user",
            target_user_id=user.id,
            target_username=user.username,
            ip=ip,
            user_agent=user_agent,
        )
        return CreateUserResponse(id=user.id, username=user.username, email=user.email)

    async def reset_password(
        self,
        *,
        target_id: int,
        actor_id: int,
        ip: str,
        user_agent: str | None,
    ) -> None:
        target = await self._users.get_by_id(target_id)
        if target is None:
            raise NotFoundError()
        if target.is_admin:
            raise CannotResetAdminError("Cannot reset super-admin password")

        await self._users.reset_password(target_id)
        # Force-logout every active session of the target.
        await self._sessions.revoke_all_for_user(target_id)

        await self._audit.log(
            actor_user_id=actor_id,
            action="reset_password",
            target_user_id=target.id,
            target_username=target.username,
            ip=ip,
            user_agent=user_agent,
        )

    async def delete_user(
        self,
        *,
        target_id: int,
        actor_id: int,
        ip: str,
        user_agent: str | None,
    ) -> DeleteUserResponse:
        target = await self._users.get_by_id(target_id)
        if target is None:
            raise NotFoundError()
        if target.is_admin:
            raise CannotDeleteAdminError("Cannot delete super-admin")

        # Capture stats + S3 keys BEFORE the cascade.
        msgs_n, atts_n, accs_n = await self._messages.stats_for_user(target_id)
        keys = await self._messages.select_attachment_keys_for_user(target_id)

        # Revoke sessions first so any in-flight request from the user fails
        # auth on its next round-trip.
        await self._sessions.revoke_all_for_user(target_id)

        # CASCADE delete in Postgres.
        # Documented race per ``docs/05-modules.md`` sec. 8: a request that
        # creates a new session for the same ``target_id`` *between*
        # ``revoke_all_for_user`` above and ``users.delete`` here is
        # acceptable — the next round-trip will get ``not_authenticated``
        # because the user row is gone. We do not hold a row-level lock for
        # the millisecond-wide window because the cost (deadlock risk on
        # heavily-touched ``users`` rows) outweighs the benefit.
        await self._users.delete(target_id)

        # Best-effort MinIO cleanup. Two passes:
        # 1. Known keys we just collected.
        # 2. Anything left under the user prefix (defence in depth).
        if keys:
            await self._storage.delete_objects(keys)
        await self._storage.delete_prefix(f"{target_id}/")

        await self._audit.log(
            actor_user_id=actor_id,
            action="delete_user",
            target_user_id=target.id,
            target_username=target.username,
            details={
                "deleted_messages": msgs_n,
                "deleted_attachments": atts_n,
                "deleted_mail_accounts": accs_n,
            },
            ip=ip,
            user_agent=user_agent,
        )
        return DeleteUserResponse(
            ok=True,
            deleted_attachments=atts_n,
            deleted_messages=msgs_n,
            deleted_mail_accounts=accs_n,
        )

    # --- Audit -------------------------------------------------------------

    async def list_audit(
        self,
        *,
        action: str | None,
        target_user_id: int | None,
        from_date: datetime | None,
        to_date: datetime | None,
        page: int,
        limit: int,
    ) -> AuditListResponse:
        items, total = await self._audit_repo.list_paged(
            action=action,
            target_user_id=target_user_id,
            from_date=from_date,
            to_date=to_date,
            page=page,
            limit=limit,
        )
        return AuditListResponse(
            items=[
                AuditEntryDTO(
                    id=a.id,
                    actor_user_id=a.actor_user_id,
                    action=a.action,
                    target_user_id=a.target_user_id,
                    target_username=a.target_username,
                    details=a.details,
                    ip=a.ip,
                    created_at=a.created_at,
                )
                for a in items
            ],
            total=total,
            page=page,
            limit=limit,
        )
