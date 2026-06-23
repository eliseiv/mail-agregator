"""GroupsService — list/create/rename/delete + auto-create on leader create.

See ADR-0019 §5/§6/§9. All mutating endpoints are super-admin only;
audit log captures every action.
"""

from __future__ import annotations

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.audit import AuditWriter
from backend.app.deps import VisibilityScope
from backend.app.exceptions import (
    ConflictError,
    NotFoundError,
    ValidationError,
)
from backend.app.groups.schemas import (
    EligibleUserDTO,
    EligibleUsersResponse,
    GroupDetailDTO,
    GroupDTO,
    GroupsListResponse,
    UserBriefDTO,
)
from backend.app.repositories.groups import GroupsRepo
from backend.app.repositories.user_groups import UserGroupsRepo
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

log = get_logger(__name__)


def _user_brief(u: User) -> UserBriefDTO:
    return UserBriefDTO(
        id=u.id,
        username=u.username,
        display_name=u.display_name,
        role=u.role,
    )


def _auto_group_name(user: User) -> str:
    """Build the auto-generated group name for a new leader.

    Per ADR-0019 §5: ``"Команда {display_name | username}"``. Always ≤ 100
    characters because both source fields are length-bounded.
    """
    label = (user.display_name or user.username).strip()
    return f"Команда {label}"[:100]


class GroupsService:
    def __init__(self, session: AsyncSession) -> None:
        self._db = session
        self._repo = GroupsRepo(session)
        self._users = UsersRepo(session)
        self._memberships = UserGroupsRepo(session)
        self._audit = AuditWriter(session)
        self._sessions = SessionStore()

    # --- Reads -------------------------------------------------------------

    async def list_for_scope(
        self,
        scope: VisibilityScope,
        *,
        q: str | None,
        page: int,
        limit: int,
    ) -> GroupsListResponse:
        """List groups visible to the caller.

        - super_admin: every group.
        - group_leader / group_member: only the caller's own group (if any).

        The output schema is the same in either case so the same template
        can render both views.
        """
        if scope.is_super_admin:
            groups, total = await self._repo.list_all(q=q, page=page, limit=limit)
        else:
            if scope.group_id is None:
                return GroupsListResponse(items=[], total=0, page=page, limit=limit)
            own = await self._repo.get_by_id(scope.group_id)
            groups = [own] if own is not None else []
            total = len(groups)

        ids = [g.id for g in groups]
        leaders_map = await self._repo.get_leaders_bulk(ids)
        counts_map = await self._repo.member_counts_bulk(ids)

        items: list[GroupDTO] = []
        for g in groups:
            leader = leaders_map.get(g.id)
            # FE-FIX round-2 #3: orphan groups (no leader yet) — leader brief is None.
            items.append(
                GroupDTO(
                    id=g.id,
                    name=g.name,
                    leader=_user_brief(leader) if leader is not None else None,
                    members_count=counts_map.get(g.id, 0),
                    created_at=g.created_at,
                )
            )
        return GroupsListResponse(items=items, total=total, page=page, limit=limit)

    async def get_detail(self, scope: VisibilityScope, group_id: int) -> GroupDetailDTO:
        group = await self._repo.get_by_id(group_id)
        if group is None:
            raise NotFoundError("group_not_found")
        if not scope.is_super_admin and scope.group_id != group_id:
            # Hide existence from non-members.
            raise NotFoundError("group_not_found")
        # FE-FIX round-2 #3: orphan group (no leader yet) — leader brief is None.
        leader = None
        if group.leader_user_id is not None:
            leader = await self._users.get_by_id(group.leader_user_id)
            if leader is None:
                log.warning("group_without_leader", group_id=group.id)
        # ADR-0030: list members via ``user_groups`` (home + additional)
        # so the group card reflects everyone who can see the team's mail.
        members = await self._repo.list_members_in_group(group.id)
        return GroupDetailDTO(
            id=group.id,
            name=group.name,
            leader=_user_brief(leader) if leader is not None else None,
            members=[_user_brief(m) for m in members],
            created_at=group.created_at,
        )

    # --- Auto-create flow used by AdminService.create_user ----------------

    async def create_for_leader(
        self,
        *,
        leader_user_id: int,
        name: str,
        actor_user_id: int,
        ip: str | None,
        user_agent: str | None,
        auto_created: bool,
    ) -> Group:
        """Create a group and bind ``leader_user_id`` as the leader.

        Sequence (single transaction managed by the caller):

        1. INSERT into ``groups`` (FK ``leader_user_id`` already exists).
        2. UPDATE the user row: ``role='group_leader'`` + ``group_id=<new>``.

        Per ADR-0019 §6 the FK on ``users.group_id`` is DEFERRABLE so the
        order is permissive even when the leader was just inserted.
        """
        # Snapshot the previous home team before the UPDATE so we can drop
        # its mirrored membership (ADR-0030 — home membership is always
        # mirrored, and a leader's home is the team they lead).
        before = await self._users.get_by_id(leader_user_id)
        old_home_group_id = before.group_id if before is not None else None

        try:
            group = await self._repo.insert(name=name, leader_user_id=leader_user_id)
        except IntegrityError as exc:
            raise ConflictError(
                "User already leads another group",
                field="leader_user_id",
            ) from exc

        await self._users.update_fields(
            leader_user_id,
            role=ROLE_GROUP_LEADER,
            group_id=group.id,
        )

        # ADR-0030: mirror the new home membership; drop the stale one.
        if old_home_group_id is not None and old_home_group_id != group.id:
            await self._memberships.remove(user_id=leader_user_id, group_id=old_home_group_id)
        await self._memberships.add(user_id=leader_user_id, group_id=group.id)

        await self._audit.log(
            actor_user_id=actor_user_id,
            action="group_create",
            target_user_id=leader_user_id,
            details={
                "group_id": group.id,
                "group_name": group.name,
                "auto_created": auto_created,
            },
            ip=ip,
            user_agent=user_agent,
        )
        return group

    # --- Public mutations (super_admin) ------------------------------------

    async def create(
        self,
        *,
        actor: VisibilityScope,
        name: str,
        leader_user_id: int | None,
        member_ids: list[int] | None = None,
        ip: str | None,
        user_agent: str | None,
    ) -> GroupDetailDTO:
        """Create a group with the given leader and optional initial members.

        FE-FIX round-2 #3: ``leader_user_id`` is optional. If null, the
        first user in ``member_ids`` (if any) becomes the leader; if both
        are empty, the group is created leaderless. The first member
        added later through a separate flow then becomes the leader.

        Atomic: all writes (insert group, promote leader, demote/add
        members, audit rows, session revocations) happen inside the
        caller's open transaction. If any validation fails the whole
        request rolls back.
        """
        if not actor.is_super_admin:
            raise NotFoundError("group_not_found")  # hide endpoint existence

        member_ids = list(member_ids or [])

        # FE-FIX round-2 #3: if leader is omitted but members are given,
        # promote the first member to leader (the rest stay as members).
        if leader_user_id is None and member_ids:
            leader_user_id = member_ids[0]
            member_ids = member_ids[1:]

        if leader_user_id is not None and leader_user_id in member_ids:
            raise ValidationError(
                "leader_user_id must not appear in member_ids",
                field="member_ids",
            )

        target: User | None = None
        if leader_user_id is not None:
            target = await self._users.get_by_id(leader_user_id)
            if target is None:
                raise NotFoundError("user_not_found")
            if target.role == ROLE_SUPER_ADMIN:
                raise ValidationError(
                    "Super admin cannot lead a group",
                    field="leader_user_id",
                )
            existing_group = await self._repo.get_by_leader(leader_user_id)
            if existing_group is not None:
                raise ConflictError(
                    "User already leads another group",
                    field="leader_user_id",
                )

        # Pre-validate every member id before mutating state. Doing the
        # whole check first means failures surface as clean 400s instead
        # of triggering a partial-write rollback.
        members: list[User] = []
        if member_ids:
            members_map = await self._users.get_many_by_ids(member_ids)
            missing = [mid for mid in member_ids if mid not in members_map]
            if missing:
                raise ValidationError(
                    "One or more member_ids do not exist",
                    field="member_ids",
                    details={"missing": missing},
                )
            for mid in member_ids:
                m = members_map[mid]
                if m.role == ROLE_SUPER_ADMIN:
                    raise ValidationError(
                        "Super admin cannot be a group member",
                        field="member_ids",
                        details={"user_id": mid},
                    )
                # If the user is currently leader of some other group,
                # the FK ON DELETE RESTRICT and our own invariants make
                # the demotion ambiguous (their old group becomes
                # leaderless). Reject up-front; super-admin must delete
                # the old group first.
                led = await self._repo.get_by_leader(mid)
                if led is not None:
                    raise ValidationError(
                        "Cannot add an existing leader as member; " "delete their group first",
                        field="member_ids",
                        details={"user_id": mid, "led_group_id": led.id},
                    )
                members.append(m)

        # FE-FIX round-2 #3: orphan group (no leader, no members) — just
        # insert the row; admin can wire up the leader later.
        if leader_user_id is None:
            group = await self._repo.insert(name=name, leader_user_id=None)
            await self._audit.log(
                actor_user_id=actor.user_id,
                action="group_create",
                target_user_id=None,
                target_username=None,
                details={"group_id": group.id, "auto_created": False, "leaderless": True},
                ip=ip,
                user_agent=user_agent,
            )
            return await self.get_detail(actor, group.id)

        # Capture the pre-mutation snapshot before any UPDATE — the same
        # SQLAlchemy session is used by ``create_for_leader``, which expires
        # ``target`` after its UPDATE so subsequent ``target.role`` reads
        # would actually re-fetch the post-UPDATE values.
        assert target is not None  # leader_user_id resolved above implies target is set
        leader_username_before = target.username
        leader_role_before = target.role
        leader_group_before = target.group_id

        # 1. Create the group + promote the leader.
        group = await self.create_for_leader(
            leader_user_id=leader_user_id,
            name=name,
            actor_user_id=actor.user_id,
            ip=ip,
            user_agent=user_agent,
            auto_created=False,
        )
        # Force-logout target so its session picks up the new role+group.
        await self._sessions.revoke_all_for_user(leader_user_id)
        await self._audit.log(
            actor_user_id=actor.user_id,
            action="user_role_change",
            target_user_id=leader_user_id,
            target_username=leader_username_before,
            details={
                "from_role": leader_role_before,
                "to_role": ROLE_GROUP_LEADER,
                "group_id_before": leader_group_before,
                "group_id_after": group.id,
            },
            ip=ip,
            user_agent=user_agent,
        )

        # 2. For each member: set role=group_member + group_id=<new>.
        for m in members:
            # Snapshot before mutation (see leader-side comment above).
            member_id = m.id
            member_username = m.username
            old_role = m.role
            old_group = m.group_id
            await self._users.update_fields(
                member_id,
                role=ROLE_GROUP_MEMBER,
                group_id=group.id,
            )
            # ADR-0030: mirror the new home membership; drop the stale one.
            if old_group is not None and old_group != group.id:
                await self._memberships.remove(user_id=member_id, group_id=old_group)
            await self._memberships.add(user_id=member_id, group_id=group.id)
            await self._sessions.revoke_all_for_user(member_id)
            # Use ``user_role_change`` if the role actually changed; else
            # ``user_group_change``. Both are existing audit actions
            # (see ADR-0019 §9 + 04-api-contracts.md).
            if old_role != ROLE_GROUP_MEMBER:
                await self._audit.log(
                    actor_user_id=actor.user_id,
                    action="user_role_change",
                    target_user_id=member_id,
                    target_username=member_username,
                    details={
                        "from_role": old_role,
                        "to_role": ROLE_GROUP_MEMBER,
                        "group_id_before": old_group,
                        "group_id_after": group.id,
                    },
                    ip=ip,
                    user_agent=user_agent,
                )
            elif old_group != group.id:
                await self._audit.log(
                    actor_user_id=actor.user_id,
                    action="user_group_change",
                    target_user_id=member_id,
                    target_username=member_username,
                    details={
                        "from_group_id": old_group,
                        "to_group_id": group.id,
                    },
                    ip=ip,
                    user_agent=user_agent,
                )

        return await self.get_detail(actor, group.id)

    async def list_eligible_users(
        self,
        actor: VisibilityScope,
    ) -> EligibleUsersResponse:
        """Return users that may be picked as leader / member in a new group.

        Excludes the super-admin (they can never be a group member or
        leader; ADR-0019 §6 invariants). Result is intentionally small —
        the project caps total users at ≤ 5 — so a single SELECT without
        pagination is fine.
        """
        if not actor.is_super_admin:
            raise NotFoundError("group_not_found")  # hide endpoint existence

        users, _total = await self._users.list_paged(
            q=None,
            page=1,
            limit=200,
        )
        # Bulk-load the groups referenced by these users so we can embed
        # ``{id, name}`` without an N+1 lookup.
        gids = sorted({u.group_id for u in users if u.group_id is not None})
        groups = await self._repo.list_by_ids(gids)
        group_by_id: dict[int, Group] = {g.id: g for g in groups}

        items: list[EligibleUserDTO] = []
        for u in users:
            if u.role == ROLE_SUPER_ADMIN:
                continue
            grp_payload: dict[str, str | int] | None = None
            if u.group_id is not None:
                g = group_by_id.get(u.group_id)
                if g is not None:
                    grp_payload = {"id": g.id, "name": g.name}
            items.append(
                EligibleUserDTO(
                    id=u.id,
                    username=u.username,
                    display_name=u.display_name,
                    role=u.role,
                    group=grp_payload,
                )
            )
        return EligibleUsersResponse(items=items)

    async def rename(
        self,
        *,
        actor: VisibilityScope,
        group_id: int,
        name: str,
        ip: str | None,
        user_agent: str | None,
    ) -> GroupDetailDTO:
        # FE-FIX round-5 #1: group_leader can rename their own group.
        # super_admin can rename any group.
        group = await self._repo.get_by_id(group_id)
        if group is None:
            raise NotFoundError("group_not_found")
        if not actor.is_super_admin:
            is_own_leader = (
                actor.role == ROLE_GROUP_LEADER
                and actor.group_id == group_id
                and group.leader_user_id == actor.user_id
            )
            if not is_own_leader:
                raise NotFoundError("group_not_found")
        from_name = group.name
        await self._repo.rename(group_id=group_id, name=name)
        await self._audit.log(
            actor_user_id=actor.user_id,
            action="group_rename",
            target_user_id=group.leader_user_id,
            details={
                "group_id": group_id,
                "from_name": from_name,
                "to_name": name,
            },
            ip=ip,
            user_agent=user_agent,
        )
        return await self.get_detail(actor, group_id)

    async def delete(
        self,
        *,
        actor: VisibilityScope,
        group_id: int,
        ip: str | None,
        user_agent: str | None,
    ) -> None:
        if not actor.is_super_admin:
            raise NotFoundError("group_not_found")
        group = await self._repo.get_by_id(group_id)
        if group is None:
            raise NotFoundError("group_not_found")

        # Refuse if any members still belong (incl. leader). Super-admin
        # must redistribute via PATCH /api/admin/users first.
        members = await self._users.list_user_ids_in_group(group_id)
        if members:
            raise ValidationError(
                "Cannot delete a group with members",
                field="group_id",
                details={"members_count": len(members)},
            )

        # Belt-and-braces: the leader's row must already have been moved.
        # `users_role_group_invariant` would otherwise be tripped via the
        # SET NULL cascade. We additionally drop the leader's role to
        # ``group_member`` (and group to NULL) only if the user *was* the
        # leader and somehow still references this group — in practice this
        # branch is unreachable because ``members`` above already covered it.

        await self._repo.delete(group_id)
        await self._audit.log(
            actor_user_id=actor.user_id,
            action="group_delete",
            target_user_id=group.leader_user_id,
            details={"group_id": group_id, "group_name": group.name},
            ip=ip,
            user_agent=user_agent,
        )

    # --- Used by AdminService when toggling role/group --------------------

    async def ensure_member_role(self, target_user_id: int, group_id: int) -> None:
        """Assert the target's group exists and target becomes a regular member."""
        group = await self._repo.get_by_id(group_id)
        if group is None:
            raise ValidationError("group_not_found", field="group_id")
        await self._users.update_fields(
            target_user_id,
            role=ROLE_GROUP_MEMBER,
            group_id=group_id,
        )
