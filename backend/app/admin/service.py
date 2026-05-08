"""AdminService — list/create/update/reset/delete users + audit log.

Post-ADR-0019: visibility is governed by :class:`VisibilityScope` and
write operations are role-aware (super_admin / group_leader).

- ``super_admin`` can create / update / reset / delete any non-admin
  user. Creating a ``group_leader`` auto-creates the group (ADR-0019 §5).
- ``group_leader`` can create / reset / delete only ``group_member`` users
  inside their own group; ``role`` and ``group_id`` are forced to the
  caller's group regardless of the payload.
- ``group_member`` cannot use any admin endpoint (403).
"""

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
    GroupBriefDTO,
    UpdateUserRequest,
    UserDTO,
    UserMailAccountSummary,
    UsersListResponse,
)
from backend.app.audit import AuditWriter
from backend.app.deps import VisibilityScope
from backend.app.exceptions import (
    CannotDeleteAdminError,
    CannotResetAdminError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    ValidationError,
)
from backend.app.groups.service import GroupsService, _auto_group_name
from backend.app.repositories.audit import AuditRepo
from backend.app.repositories.groups import GroupsRepo
from backend.app.repositories.mail_accounts import MailAccountsRepo
from backend.app.repositories.messages import MessagesRepo
from backend.app.repositories.users import UsersRepo
from backend.app.sessions import SessionStore
from shared.logging import get_logger
from shared.models import (
    ROLE_GROUP_LEADER,
    ROLE_GROUP_MEMBER,
    ROLE_SUPER_ADMIN,
    Group,
    User,
)
from shared.storage import get_storage

log = get_logger(__name__)


def _group_brief(group: Group | None) -> GroupBriefDTO | None:
    if group is None:
        return None
    return GroupBriefDTO(id=group.id, name=group.name)


class AdminService:
    def __init__(self, session: AsyncSession) -> None:
        self._db = session
        self._users = UsersRepo(session)
        self._groups = GroupsRepo(session)
        self._accounts = MailAccountsRepo(session)
        self._messages = MessagesRepo(session)
        self._audit_repo = AuditRepo(session)
        self._audit = AuditWriter(session)
        self._sessions = SessionStore()
        self._storage = get_storage()

    # --- Users -------------------------------------------------------------

    async def list_users(
        self,
        actor: VisibilityScope,
        *,
        q: str | None,
        page: int,
        limit: int,
        group_id: int | None = None,
        role: str | None = None,
    ) -> UsersListResponse:
        """List users visible to ``actor``.

        - super_admin: every user; optional ``group_id`` / ``role`` filter.
        - group_leader / group_member: only users inside the caller's group.
        """
        if actor.is_super_admin:
            in_group_ids: list[int] | None = None
        else:
            if actor.group_id is None:
                return UsersListResponse(items=[], total=0, page=page, limit=limit)
            in_group_ids = [actor.group_id]
            # Lock the group_id filter to the caller's own group.
            if group_id is not None and group_id != actor.group_id:
                raise ForbiddenError("user_not_in_group_scope")
            group_id = actor.group_id

        users, total = await self._users.list_paged(
            q,
            page,
            limit,
            group_id=group_id,
            role=role,
            in_group_ids=in_group_ids,
        )
        accs_map = await self._accounts.list_for_users([u.id for u in users])

        # Group lookup (bulk) for the embedded brief.
        gids = sorted({u.group_id for u in users if u.group_id is not None})
        groups = await self._groups.list_by_ids(gids)
        group_by_id: dict[int, Group] = {g.id: g for g in groups}

        items = [
            UserDTO(
                id=u.id,
                username=u.username,
                email=u.email,
                display_name=u.display_name,
                role=u.role,
                group=_group_brief(group_by_id.get(u.group_id) if u.group_id is not None else None),
                password_reset_required=u.password_reset_required,
                lockout_until=u.lockout_until,
                last_login_at=u.last_login_at,
                created_at=u.created_at,
                mail_accounts=[
                    UserMailAccountSummary(
                        id=a.id,
                        email=a.email,
                        display_name=a.display_name,
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

    async def _resolve_create_role_and_group(
        self,
        actor: VisibilityScope,
        payload: CreateUserRequest,
    ) -> tuple[str, int | None]:
        """Apply role-aware authorisation rules and return the
        ``(role, group_id)`` that will actually be persisted.

        - super_admin: payload.role + payload.group_id (validated by the
          schema's ``model_validator``).
        - group_leader: forced to ``role='group_member'`` + own group.
        - group_member: 403.
        """
        if actor.is_super_admin:
            if payload.role == ROLE_GROUP_LEADER:
                # Auto-create the group → group_id is None for now.
                return ROLE_GROUP_LEADER, None
            # FE-FIX round-2 #4: group_id is optional for group_member —
            # super-admin can create user без group и привязать позже.
            if payload.group_id is None:
                return ROLE_GROUP_MEMBER, None
            group = await self._groups.get_by_id(payload.group_id)
            if group is None:
                raise ValidationError("group_not_found", field="group_id")
            return ROLE_GROUP_MEMBER, payload.group_id

        if actor.role == ROLE_GROUP_LEADER:
            if payload.role == ROLE_GROUP_LEADER:
                raise ForbiddenError("Leaders cannot create other leaders")
            assert actor.group_id is not None  # invariant
            return ROLE_GROUP_MEMBER, actor.group_id

        # group_member or unknown role:
        raise ForbiddenError("Users cannot create users")

    async def create_user(
        self,
        *,
        actor: VisibilityScope,
        payload: CreateUserRequest,
        ip: str,
        user_agent: str | None,
    ) -> CreateUserResponse:
        role, group_id = await self._resolve_create_role_and_group(actor, payload)

        try:
            # ``email`` is no longer accepted in the request payload (the
            # field was removed from the public API). New users are created
            # with ``email = NULL``; the DB column itself is kept for
            # backwards compatibility with the seeded super-admin and old
            # rows. See ``CreateUserRequest`` docstring.
            user = await self._users.create(
                username=payload.username,
                email=None,
                role=role,
                group_id=group_id,
                display_name=payload.display_name,
                password_hash=None,
                password_reset_required=True,
            )
        except IntegrityError as exc:
            raise ConflictError("Username already exists", field="username") from exc

        # Audit base event.
        await self._audit.log(
            actor_user_id=actor.user_id,
            action="create_user",
            target_user_id=user.id,
            target_username=user.username,
            details={"role": role, "group_id": group_id},
            ip=ip,
            user_agent=user_agent,
        )

        # Auto-create group flow for new leader.
        group: Group | None = None
        if role == ROLE_GROUP_LEADER:
            group = await GroupsService(self._db).create_for_leader(
                leader_user_id=user.id,
                name=_auto_group_name(user),
                actor_user_id=actor.user_id,
                ip=ip,
                user_agent=user_agent,
                auto_created=True,
            )
            # Refresh user attrs so the response reflects the new group.
            user = await self._users.get_by_id(user.id) or user
        elif group_id is not None:
            group = await self._groups.get_by_id(group_id)

        return CreateUserResponse(
            id=user.id,
            username=user.username,
            email=user.email,
            display_name=user.display_name,
            role=user.role,
            group_id=user.group_id,
            group=_group_brief(group),
        )

    # --- Update -----------------------------------------------------------

    async def update_user(
        self,
        *,
        actor: VisibilityScope,
        target_id: int,
        payload: UpdateUserRequest,
        ip: str,
        user_agent: str | None,
    ) -> UserDTO:
        target = await self._users.get_by_id(target_id)
        if target is None:
            raise NotFoundError()
        if target.role == ROLE_SUPER_ADMIN:
            raise ForbiddenError("Cannot modify super_admin")

        # Authorisation by role.
        if actor.is_super_admin:
            pass
        elif actor.role == ROLE_GROUP_LEADER:
            if target.group_id != actor.group_id:
                raise ForbiddenError("user_not_in_group_scope")
            # Leaders may only adjust display_name on members of their group.
            if payload.role is not None or payload.group_id is not None or payload.clear_group_id:
                raise ForbiddenError("Leaders cannot change role or group")
        else:
            raise ForbiddenError("Forbidden")

        # Sentinel to distinguish "leave display_name unchanged" from
        # "set display_name to NULL". ``Ellipsis`` reads cleanly at the
        # call site (``if x is ...``) and avoids a third state-flag arg.
        _UNSET: object = object()
        new_display_name: str | None | object = _UNSET
        if payload.clear_display_name:
            new_display_name = None
        elif payload.display_name is not None:
            new_display_name = payload.display_name

        old_role = target.role
        old_group = target.group_id

        new_role: str | None = payload.role
        new_group_id: int | None = payload.group_id
        clear_group = payload.clear_group_id

        # Super-admin role/group transitions.
        role_changed = False
        group_changed = False

        if actor.is_super_admin:
            if new_role is not None and new_role != old_role:
                role_changed = True
                if new_role == ROLE_GROUP_LEADER:
                    # Promotion to leader → auto-create group.
                    if new_group_id is not None or clear_group:
                        raise ValidationError(
                            "group_id_must_be_null_for_new_leader",
                            field="group_id",
                        )
                    # Step 1: detach from old group + null role temporarily —
                    # the consistency trigger is DEFERRABLE so the COMMIT-time
                    # check sees the final state.
                    await self._users.update_fields(
                        target_id,
                        role=ROLE_GROUP_LEADER,
                        group_id=None,
                    )
                    fresh = await self._users.get_by_id(target_id)
                    assert fresh is not None
                    await GroupsService(self._db).create_for_leader(
                        leader_user_id=target_id,
                        name=_auto_group_name(fresh),
                        actor_user_id=actor.user_id,
                        ip=ip,
                        user_agent=user_agent,
                        auto_created=True,
                    )
                elif new_role == ROLE_GROUP_MEMBER:
                    # Demotion: must explicitly assign a target group_id.
                    if old_role == ROLE_GROUP_LEADER:
                        # Leader cannot self-demote without first moving the
                        # group; we surface the canonical error.
                        raise ValidationError(
                            "cannot_demote_lone_leader",
                            field="role",
                            details={"hint": "Delete or reassign the group first"},
                        )
                    if new_group_id is None and not clear_group:
                        # No-op if same role, but caught earlier.
                        raise ValidationError(
                            "group_id is required when demoting to group_member",
                            field="group_id",
                        )
                    if new_group_id is None:
                        raise ValidationError(
                            "group_id is required when demoting to group_member",
                            field="group_id",
                        )
                    group = await self._groups.get_by_id(new_group_id)
                    if group is None:
                        raise ValidationError("group_not_found", field="group_id")
                    await self._users.update_fields(
                        target_id,
                        role=ROLE_GROUP_MEMBER,
                        group_id=new_group_id,
                    )
                    group_changed = old_group != new_group_id
            elif new_group_id is not None and new_group_id != old_group:
                # Same role, change of group (allowed only for group_member).
                if old_role != ROLE_GROUP_MEMBER:
                    raise ValidationError(
                        "Only group_member may change group_id without role change",
                        field="group_id",
                    )
                group = await self._groups.get_by_id(new_group_id)
                if group is None:
                    raise ValidationError("group_not_found", field="group_id")
                await self._users.update_fields(
                    target_id,
                    group_id=new_group_id,
                )
                group_changed = True

        # Apply display_name change (any role gate already passed).
        if new_display_name is not _UNSET:
            # ``new_display_name`` here is one of: a non-empty str, or None
            # (clear) — the ``_UNSET`` branch was filtered above.
            await self._users.update_fields(
                target_id,
                display_name=new_display_name,
            )

        # Refresh & build DTO.
        refreshed = await self._users.get_by_id(target_id)
        assert refreshed is not None

        # If role or group changed → revoke sessions of the target.
        if role_changed or group_changed:
            await self._sessions.revoke_all_for_user(target_id)
            audit_action: str
            if role_changed:
                audit_action = "user_role_change"
                details = {
                    "from_role": old_role,
                    "to_role": refreshed.role,
                    "group_id_before": old_group,
                    "group_id_after": refreshed.group_id,
                }
            else:
                audit_action = "user_group_change"
                details = {
                    "from_group_id": old_group,
                    "to_group_id": refreshed.group_id,
                }
            await self._audit.log(
                actor_user_id=actor.user_id,
                action=audit_action,
                target_user_id=target_id,
                target_username=refreshed.username,
                details=details,
                ip=ip,
                user_agent=user_agent,
            )

        # Compose response.
        group = (
            await self._groups.get_by_id(refreshed.group_id)
            if refreshed.group_id is not None
            else None
        )
        accs = await self._accounts.list_for_users([refreshed.id])
        return UserDTO(
            id=refreshed.id,
            username=refreshed.username,
            email=refreshed.email,
            display_name=refreshed.display_name,
            role=refreshed.role,
            group=_group_brief(group),
            password_reset_required=refreshed.password_reset_required,
            lockout_until=refreshed.lockout_until,
            last_login_at=refreshed.last_login_at,
            created_at=refreshed.created_at,
            mail_accounts=[
                UserMailAccountSummary(
                    id=a.id,
                    email=a.email,
                    display_name=a.display_name,
                    is_active=a.is_active,
                    last_synced_at=a.last_synced_at,
                    last_sync_error=a.last_sync_error,
                )
                for a in accs.get(refreshed.id, [])
            ],
        )

    # --- Reset + delete --------------------------------------------------

    async def _assert_can_act_on(self, actor: VisibilityScope, target: User) -> None:
        if target.role == ROLE_SUPER_ADMIN:
            raise CannotResetAdminError("Cannot operate on super-admin")
        if actor.is_super_admin:
            return
        if actor.role == ROLE_GROUP_LEADER:
            if target.group_id != actor.group_id:
                raise ForbiddenError("user_not_in_group_scope")
            if target.role == ROLE_GROUP_LEADER:
                raise ForbiddenError("Leader cannot operate on another leader")
            return
        raise ForbiddenError("Forbidden")

    async def reset_password(
        self,
        *,
        actor: VisibilityScope,
        target_id: int,
        ip: str,
        user_agent: str | None,
    ) -> None:
        target = await self._users.get_by_id(target_id)
        if target is None:
            raise NotFoundError()
        if target.role == ROLE_SUPER_ADMIN:
            raise CannotResetAdminError("Cannot reset super-admin password")
        await self._assert_can_act_on(actor, target)

        await self._users.reset_password(target_id)
        await self._sessions.revoke_all_for_user(target_id)

        await self._audit.log(
            actor_user_id=actor.user_id,
            action="reset_password",
            target_user_id=target.id,
            target_username=target.username,
            ip=ip,
            user_agent=user_agent,
        )

    async def delete_user(
        self,
        *,
        actor: VisibilityScope,
        target_id: int,
        ip: str,
        user_agent: str | None,
    ) -> DeleteUserResponse:
        target = await self._users.get_by_id(target_id)
        if target is None:
            raise NotFoundError()
        if target.role == ROLE_SUPER_ADMIN:
            raise CannotDeleteAdminError("Cannot delete super-admin")
        await self._assert_can_act_on(actor, target)

        # group_leader cannot be deleted while still leading a group:
        # ``groups.leader_user_id`` has ON DELETE RESTRICT. Surface a
        # readable error instead of bubbling the IntegrityError up.
        if target.role == ROLE_GROUP_LEADER:
            led_group = await self._groups.get_by_leader(target_id)
            if led_group is not None:
                raise ValidationError(
                    "Cannot delete a leader while their group exists",
                    field="user_id",
                    details={"group_id": led_group.id},
                )

        msgs_n, atts_n, accs_n = await self._messages.stats_for_user(target_id)
        keys = await self._messages.select_attachment_keys_for_user(target_id)

        await self._sessions.revoke_all_for_user(target_id)

        # Documented race: a request that creates a new session for the
        # same ``target_id`` between revoke_all_for_user above and the
        # delete below will be invalidated on its next round-trip because
        # the user row is gone. See ``docs/05-modules.md`` sec. 8.
        await self._users.delete(target_id)

        if keys:
            await self._storage.delete_objects(keys)
        await self._storage.delete_prefix(f"{target_id}/")

        await self._audit.log(
            actor_user_id=actor.user_id,
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
