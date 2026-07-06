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

from argon2 import PasswordHasher
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.admin.schemas import (
    AuditEntryDTO,
    AuditListResponse,
    CreateUserRequest,
    CreateUserResponse,
    DeleteUserResponse,
    GroupBriefDTO,
    MembershipDTO,
    UpdateUserRequest,
    UserDTO,
    UserMailAccountSummary,
    UsersListResponse,
)
from backend.app.audit import AuditWriter
from backend.app.deps import VisibilityScope
from backend.app.exceptions import (
    CannotAddSuperAdminToGroupError,
    CannotDeleteAdminError,
    CannotMoveGroupLeaderError,
    CannotRemoveHomeMembershipError,
    CannotResetAdminError,
    ConflictError,
    ForbiddenError,
    GroupNotFoundError,
    MembershipAlreadyExistsError,
    MembershipNotFoundError,
    NotFoundError,
    PasswordNotSetError,
    ValidationError,
)
from backend.app.groups.service import GroupsService, _auto_group_name
from backend.app.repositories.audit import AuditRepo
from backend.app.repositories.groups import GroupsRepo
from backend.app.repositories.mail_accounts import MailAccountsRepo
from backend.app.repositories.messages import MessagesRepo
from backend.app.repositories.user_groups import UserGroupsRepo
from backend.app.repositories.users import UsersRepo
from backend.app.sessions import SessionStore
from backend.app.telegram.sso_service import TelegramSSOService
from shared.crypto import InvalidTag, decrypt_user_password, encrypt_user_password
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

# ADR-0038 §3: hash admin-set login passwords with the same argon2 defaults
# as the auth module. Module-level so params are computed once per process.
_PH = PasswordHasher()


def _group_brief(group: Group | None) -> GroupBriefDTO | None:
    if group is None:
        return None
    return GroupBriefDTO(id=group.id, name=group.name)


class AdminService:
    def __init__(self, session: AsyncSession) -> None:
        self._db = session
        self._users = UsersRepo(session)
        self._groups = GroupsRepo(session)
        self._memberships = UserGroupsRepo(session)
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

        # ADR-0030: bulk-load every membership (home + additional) so the
        # admin page can render team chips. Read-only — no invariant changes.
        memberships_map = await self._memberships.list_group_ids_for_users([u.id for u in users])

        # Group lookup (bulk) for the embedded brief — covers both the home
        # group (``users.group_id``) and any additional membership group, so
        # chip labels resolve to real names without an N+1 fetch.
        gids = sorted(
            {u.group_id for u in users if u.group_id is not None}
            | {gid for gid_list in memberships_map.values() for gid in gid_list}
        )
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
                memberships=[
                    brief
                    for gid in memberships_map.get(u.id, [])
                    if (brief := _group_brief(group_by_id.get(gid))) is not None
                ],
                password_reset_required=u.password_reset_required,
                has_password=u.password_encrypted is not None,
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
                if payload.group_id is None:
                    # Auto-create the group → group_id is None for now.
                    return ROLE_GROUP_LEADER, None
                # Bug-fix #2: assigning a new leader to an existing orphan
                # group. The target group must exist and have no current
                # leader; otherwise we'd silently steal leadership.
                group = await self._groups.get_by_id(payload.group_id)
                if group is None:
                    raise ValidationError("group_not_found", field="group_id")
                if group.leader_user_id is not None:
                    raise ValidationError(
                        "group_already_has_leader",
                        field="group_id",
                        details={"group_id": group.id},
                    )
                return ROLE_GROUP_LEADER, payload.group_id
            # FE-FIX round-4 #4: group_id is required for group_member at
            # creation time (schema validator enforces it; defensive check here).
            if payload.group_id is None:
                raise ValidationError("group_id is required for group_member", field="group_id")
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

        # Bug-fix #2: when assigning a new leader to an existing orphan
        # group, the user row already carries the final ``role`` +
        # ``group_id`` (the FK is DEFERRABLE so the order is permissive).
        # For the auto-create path we insert with ``group_id=NULL`` and
        # let :meth:`GroupsService.create_for_leader` wire it up. The
        # ``users_role_group_invariant`` trigger validates at COMMIT.
        assign_to_orphan_group: bool = role == ROLE_GROUP_LEADER and group_id is not None
        insert_group_id = (
            group_id
            if assign_to_orphan_group
            else (group_id if role == ROLE_GROUP_MEMBER else None)
        )

        # ADR-0038 §3: optional admin-set password. When present we insert the
        # argon2 hash immediately (no user_id needed) and clear the reset flag;
        # the reversible copy needs the freshly assigned ``user.id`` for its
        # AAD, so it is written right after the INSERT below (same transaction).
        admin_set_password = payload.password is not None
        password_hash = _PH.hash(payload.password) if payload.password is not None else None

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
                group_id=insert_group_id,
                display_name=payload.display_name,
                password_hash=password_hash,
                password_reset_required=not admin_set_password,
            )
        except IntegrityError as exc:
            raise ConflictError("Username already exists", field="username") from exc

        # ADR-0038 §3: store the reversible copy now that ``user.id`` exists.
        # The plaintext is never logged; audit records the fact only (no value).
        if admin_set_password:
            assert payload.password is not None  # mypy: guarded by admin_set_password
            blob = encrypt_user_password(payload.password, user.id)
            await self._users.update_fields(user.id, password_encrypted=blob)
            await self._audit.log(
                actor_user_id=actor.user_id,
                action="user_password_set",
                target_user_id=user.id,
                target_username=user.username,
                details={},
                ip=ip,
                user_agent=user_agent,
            )

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

        # ADR-0030: mirror the home membership for a freshly created member.
        # (Leader paths below mirror inside ``create_for_leader`` /
        # the orphan-group branch.)
        if role == ROLE_GROUP_MEMBER and insert_group_id is not None:
            await self._memberships.add(user_id=user.id, group_id=insert_group_id)
            # ADR-0038 §5 / ADR-0030: additional teams beyond the home team.
            # Only honoured for group_member (leaders are bound to their own
            # team; super_admin is not creatable via the API). Same
            # transaction; dedup against the home team and among themselves;
            # a non-existent team aborts the whole create with 400.
            await self._add_additional_memberships(
                user=user,
                home_group_id=insert_group_id,
                additional_group_ids=payload.additional_group_ids,
                actor=actor,
                ip=ip,
                user_agent=user_agent,
            )

        # Auto-create / assign-to-orphan group flow for new leader.
        group: Group | None = None
        if role == ROLE_GROUP_LEADER:
            if assign_to_orphan_group:
                # Bind the existing orphan group to the freshly inserted
                # leader. ``set_leader`` honours the UNIQUE constraint on
                # ``groups.leader_user_id`` (the resolver verified the
                # group is leaderless, but a concurrent caller could race
                # — that IntegrityError surfaces as a 409 to the client).
                assert group_id is not None  # mypy: assign_to_orphan_group ⇒ group_id set
                try:
                    await self._groups.set_leader(
                        group_id=group_id,
                        leader_user_id=user.id,
                    )
                except IntegrityError as exc:
                    raise ConflictError(
                        "User already leads another group",
                        field="group_id",
                    ) from exc
                # ADR-0030: mirror the home membership for the new leader
                # (the user was inserted with this group_id as home).
                await self._memberships.add(user_id=user.id, group_id=group_id)
                await self._audit.log(
                    actor_user_id=actor.user_id,
                    action="user_role_change",
                    target_user_id=user.id,
                    target_username=user.username,
                    details={
                        "from_role": None,
                        "to_role": ROLE_GROUP_LEADER,
                        "group_id_before": None,
                        "group_id_after": group_id,
                        "leader_assigned_to_orphan_group": True,
                    },
                    ip=ip,
                    user_agent=user_agent,
                )
                group = await self._groups.get_by_id(group_id)
            else:
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
            has_password=admin_set_password,
        )

    async def _add_additional_memberships(
        self,
        *,
        user: User,
        home_group_id: int,
        additional_group_ids: list[int] | None,
        actor: VisibilityScope,
        ip: str,
        user_agent: str | None,
    ) -> None:
        """Insert extra ``user_groups`` rows for a freshly created member
        (ADR-0038 §5 / ADR-0030).

        Dedup against the home team and among themselves; validate that each
        team exists (a missing one raises ``400 group_not_found`` and, because
        the caller wraps the create in a single transaction, rolls the whole
        operation back). Writes one ``user_group_add`` audit row per team that
        was actually added (idempotent — a duplicate is a no-op, no audit).
        """
        if not additional_group_ids:
            return
        seen: set[int] = {home_group_id}
        for gid in additional_group_ids:
            if gid in seen:
                continue
            seen.add(gid)
            group = await self._groups.get_by_id(gid)
            if group is None:
                # ADR-0038 §5 / 04-api-contracts: POST /api/admin/users
                # contract requires 400 group_not_found for a bad additional
                # team (distinct from the standalone POST .../groups endpoint,
                # ADR-0030, which returns 404). The single-transaction rollback
                # is unchanged — only the status/error class differs.
                raise ValidationError(
                    "group_not_found",
                    field="additional_group_ids",
                    details={"group_id": gid},
                )
            created = await self._memberships.add(user_id=user.id, group_id=gid)
            if created:
                await self._audit.log(
                    actor_user_id=actor.user_id,
                    action="user_group_add",
                    target_user_id=user.id,
                    target_username=user.username,
                    details={"group_id": gid},
                    ip=ip,
                    user_agent=user_agent,
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
                    # ADR-0030: drop the stale home membership now (the
                    # group_id was just nulled, so ``create_for_leader`` can no
                    # longer see the previous home to remove it).
                    if old_group is not None:
                        await self._memberships.remove(user_id=target_id, group_id=old_group)
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
                    # ADR-0030: sync home membership on demotion.
                    if old_group is not None and old_group != new_group_id:
                        await self._memberships.remove(user_id=target_id, group_id=old_group)
                    await self._memberships.add(user_id=target_id, group_id=new_group_id)
                    group_changed = old_group != new_group_id
            elif new_group_id is not None and new_group_id != old_group:
                # Same role, change of home team — "move" (ADR-0030).
                # ADR-0030 §5: moving a leader would break the leader
                # invariant (their home team must be the team they lead).
                if old_role == ROLE_GROUP_LEADER:
                    raise CannotMoveGroupLeaderError(
                        "Cannot move a group leader to another team",
                        field="group_id",
                    )
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
                # ADR-0030: keep ``user_groups`` in sync with the new home
                # team. Drop the old home membership, add the new one
                # (idempotent — dedups if the new home already existed as an
                # additional membership). Additional memberships are left
                # untouched.
                if old_group is not None:
                    await self._memberships.remove(user_id=target_id, group_id=old_group)
                await self._memberships.add(user_id=target_id, group_id=new_group_id)
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

        # FE-FIX round-10: when the user goes from "no group" into a real
        # group, attach their orphan mail accounts to the new group so the
        # whole group can see them. Existing accounts that already have a
        # ``group_id`` stay with their original group — that's the whole
        # point of the round-10 model change.
        if group_changed and old_group is None and refreshed.group_id is not None:
            await self._accounts.attach_orphans_to_group(
                user_id=refreshed.id,
                group_id=refreshed.group_id,
            )

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
        # ADR-0030: include every team membership (home + additional) in the
        # PATCH response, consistent with the listing DTO.
        membership_gids = await self._memberships.list_group_ids_for_user(refreshed.id)
        membership_groups = await self._groups.list_by_ids(sorted(set(membership_gids)))
        membership_by_id = {g.id: g for g in membership_groups}
        return UserDTO(
            id=refreshed.id,
            username=refreshed.username,
            email=refreshed.email,
            display_name=refreshed.display_name,
            role=refreshed.role,
            group=_group_brief(group),
            memberships=[
                brief
                for gid in membership_gids
                if (brief := _group_brief(membership_by_id.get(gid))) is not None
            ],
            password_reset_required=refreshed.password_reset_required,
            has_password=refreshed.password_encrypted is not None,
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

    # --- Multi-group membership (ADR-0030) --------------------------------

    async def add_membership(
        self,
        *,
        actor: VisibilityScope,
        target_id: int,
        group_id: int,
        ip: str,
        user_agent: str | None,
    ) -> MembershipDTO:
        """Add an additional team membership (ADR-0030).

        super_admin only. The target must not be a super_admin. Idempotent
        via UNIQUE — a repeat add surfaces ``409 membership_already_exists``.
        Does NOT change ``users.group_id`` (home team) or ``users.role``.
        Revokes the target's sessions so ``VisibilityScope.group_ids`` is
        re-read from ``user_groups``.
        """
        if not actor.is_super_admin:
            raise ForbiddenError("Super-admin only")

        target = await self._users.get_by_id(target_id)
        if target is None:
            raise NotFoundError()
        if target.role == ROLE_SUPER_ADMIN:
            raise CannotAddSuperAdminToGroupError(
                "Cannot add super_admin to a team",
                field="group_id",
            )

        group = await self._groups.get_by_id(group_id)
        if group is None:
            raise GroupNotFoundError("Group not found", field="group_id")

        created = await self._memberships.add(user_id=target_id, group_id=group_id)
        if not created:
            raise MembershipAlreadyExistsError(
                "User already belongs to this team",
                field="group_id",
                details={"group_id": group_id},
            )

        await self._sessions.revoke_all_for_user(target_id)
        await self._audit.log(
            actor_user_id=actor.user_id,
            action="user_group_add",
            target_user_id=target_id,
            target_username=target.username,
            details={"group_id": group_id},
            ip=ip,
            user_agent=user_agent,
        )
        # Re-read the membership timestamp for the response.
        created_at = await self._memberships.get_created_at(user_id=target_id, group_id=group_id)
        assert created_at is not None  # just inserted
        return MembershipDTO(
            user_id=target_id,
            group_id=group_id,
            group=GroupBriefDTO(id=group.id, name=group.name),
            created_at=created_at,
        )

    async def remove_membership(
        self,
        *,
        actor: VisibilityScope,
        target_id: int,
        group_id: int,
        ip: str,
        user_agent: str | None,
    ) -> None:
        """Remove an additional team membership (ADR-0030).

        super_admin only. The home membership (``group_id == users.group_id``)
        cannot be removed — that is what "move" is for. A non-existent
        additional membership surfaces ``404 membership_not_found``. Revokes
        the target's sessions on success.
        """
        if not actor.is_super_admin:
            raise ForbiddenError("Super-admin only")

        target = await self._users.get_by_id(target_id)
        if target is None:
            raise NotFoundError()

        if target.group_id is not None and target.group_id == group_id:
            raise CannotRemoveHomeMembershipError(
                "Cannot remove the home team membership; use move instead",
                field="group_id",
            )

        removed = await self._memberships.remove(user_id=target_id, group_id=group_id)
        if not removed:
            raise MembershipNotFoundError(
                "No such additional membership",
                field="group_id",
                details={"group_id": group_id},
            )

        await self._sessions.revoke_all_for_user(target_id)
        await self._audit.log(
            actor_user_id=actor.user_id,
            action="user_group_remove",
            target_user_id=target_id,
            target_username=target.username,
            details={"group_id": group_id},
            ip=ip,
            user_agent=user_agent,
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
        password: str | None = None,
    ) -> None:
        target = await self._users.get_by_id(target_id)
        if target is None:
            raise NotFoundError()
        if target.role == ROLE_SUPER_ADMIN:
            raise CannotResetAdminError("Cannot reset super-admin password")
        await self._assert_can_act_on(actor, target)

        if password is not None:
            # ADR-0038 §3: admin-set reset — write the argon2 hash AND the
            # reversible copy (AAD bound to this user_id), clear the reset
            # flag. The plaintext is never logged.
            new_hash = _PH.hash(password)
            blob = encrypt_user_password(password, target_id)
            await self._users.set_password_hash(target_id, new_hash, password_encrypted=blob)
        else:
            # Force self-set: clears both hash and reversible copy → the
            # "Password" column reverts to "—".
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
        if password is not None:
            await self._audit.log(
                actor_user_id=actor.user_id,
                action="user_password_set",
                target_user_id=target.id,
                target_username=target.username,
                details={},
                ip=ip,
                user_agent=user_agent,
            )
        # ADR-0022 §1.5: a password reset invalidates the Telegram link
        # (new owner / new device assumed).
        await TelegramSSOService(self._db).revoke_for_user(
            user_id=target.id,
            reason="password_reset",
            ip=ip,
            user_agent=user_agent,
        )

    async def reveal_login_password(
        self,
        *,
        actor: VisibilityScope,
        target_id: int,
        ip: str,
        user_agent: str | None,
    ) -> str:
        """Return the decrypted login password of ``target_id`` (ADR-0038 §4).

        super_admin only (enforced by the ``SuperAdminScope`` dependency at
        the route). Raises ``404 not_found`` for a missing user and
        ``404 password_not_set`` when there is no reversible copy
        (``password_encrypted IS NULL`` → the UI column shows "—"). Every
        successful reveal writes a ``user_password_revealed`` audit row
        (``details={}`` — the value is NEVER logged or stored in audit).
        """
        target = await self._users.get_by_id(target_id)
        if target is None:
            raise NotFoundError()
        if target.password_encrypted is None:
            raise PasswordNotSetError()

        try:
            plaintext = decrypt_user_password(target.password_encrypted, target.id)
        except InvalidTag:
            # Corrupt blob or wrong/rotated key. Never log the ciphertext or
            # any secret; surface as a generic internal error (500) via the
            # unhandled handler rather than masking it as "not set".
            log.error("user_password_decrypt_failed", user_id=target.id)
            raise

        await self._audit.log(
            actor_user_id=actor.user_id,
            action="user_password_revealed",
            target_user_id=target.id,
            target_username=target.username,
            details={},
            ip=ip,
            user_agent=user_agent,
        )
        return plaintext

    async def _dissolve_leader_group(
        self,
        *,
        actor: VisibilityScope,
        target: User,
        ip: str,
        user_agent: str | None,
    ) -> None:
        """Dissolve the group led by ``target`` when ``target`` is about to
        be deleted.

        Pre: ``target.role == 'group_leader'`` and ``target.group_id`` is set.

        Behaviour:

        - If any other user still belongs to the group → raise
          :class:`ValidationError` so the operator first redistributes the
          members.
        - Otherwise: detach the leader pointer (``groups.leader_user_id =
          NULL``) so the upcoming ``DELETE FROM users`` is not blocked by
          ``ON DELETE RESTRICT``; then DELETE the now-empty group and write
          a ``group_delete`` audit row.

        The actual ``DELETE FROM users`` is performed by the caller (
        :meth:`delete_user`) right after this helper returns.
        """
        assert target.group_id is not None  # invariant: pre-condition
        led_group = await self._groups.get_by_id(target.group_id)
        if led_group is None:
            # Defensive: the user's group_id points at a deleted group.
            # Nothing to dissolve — the caller may proceed with the user
            # delete, the SET NULL FK on users.group_id already detached.
            return

        # Count members in the group excluding the leader itself.
        member_ids = await self._users.list_user_ids_in_group(led_group.id)
        other_members = [uid for uid in member_ids if uid != target.id]
        if other_members:
            raise ValidationError(
                "Сначала переведите участников команды в другую команду",
                field="role",
                details={
                    "group_id": led_group.id,
                    "members_count": len(other_members),
                },
            )

        # Detach the leader pointer first so ``DELETE FROM users`` (run by
        # the caller) is not blocked by ``ON DELETE RESTRICT`` on
        # ``groups.leader_user_id``.
        await self._groups.set_leader(group_id=led_group.id, leader_user_id=None)
        # Delete the now-empty, leaderless group.
        await self._groups.delete(led_group.id)
        await self._audit.log(
            actor_user_id=actor.user_id,
            action="group_delete",
            target_user_id=target.id,
            target_username=target.username,
            details={
                "group_id": led_group.id,
                "group_name": led_group.name,
                "auto_dissolved": True,
            },
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

        # Leader-being-deleted edge case (bug #1): ``groups.leader_user_id``
        # has ``ON DELETE RESTRICT``, so deleting a leader directly trips
        # an IntegrityError. We instead dissolve the (now-empty) group as
        # part of the same transaction — but only when there are no other
        # members; otherwise the operator must redistribute members first.
        if target.role == ROLE_GROUP_LEADER and target.group_id is not None:
            await self._dissolve_leader_group(
                actor=actor,
                target=target,
                ip=ip,
                user_agent=user_agent,
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
