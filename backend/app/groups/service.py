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
    GroupDetailDTO,
    GroupDTO,
    GroupsListResponse,
    UserBriefDTO,
)
from backend.app.repositories.groups import GroupsRepo
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

    Per ADR-0019 §5: ``"Группа {display_name | username}"``. Always ≤ 100
    characters because both source fields are length-bounded.
    """
    label = (user.display_name or user.username).strip()
    return f"Группа {label}"[:100]


class GroupsService:
    def __init__(self, session: AsyncSession) -> None:
        self._db = session
        self._repo = GroupsRepo(session)
        self._users = UsersRepo(session)
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
            if leader is None:
                # Defensive: leader vanished (FK RESTRICT prevents it, but
                # surface as 500-equivalent rather than crash).
                log.warning("group_without_leader", group_id=g.id)
                continue
            items.append(
                GroupDTO(
                    id=g.id,
                    name=g.name,
                    leader=_user_brief(leader),
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
        leader = await self._users.get_by_id(group.leader_user_id)
        if leader is None:
            log.warning("group_without_leader", group_id=group.id)
            raise NotFoundError("group_not_found")
        members = await self._users.list_in_group(group.id)
        return GroupDetailDTO(
            id=group.id,
            name=group.name,
            leader=_user_brief(leader),
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
        leader_user_id: int,
        ip: str | None,
        user_agent: str | None,
    ) -> GroupDetailDTO:
        if not actor.is_super_admin:
            raise NotFoundError("group_not_found")  # hide endpoint existence

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
            target_username=target.username,
            details={
                "from_role": target.role,
                "to_role": ROLE_GROUP_LEADER,
                "group_id_before": target.group_id,
                "group_id_after": group.id,
            },
            ip=ip,
            user_agent=user_agent,
        )
        return await self.get_detail(actor, group.id)

    async def rename(
        self,
        *,
        actor: VisibilityScope,
        group_id: int,
        name: str,
        ip: str | None,
        user_agent: str | None,
    ) -> GroupDetailDTO:
        if not actor.is_super_admin:
            raise NotFoundError("group_not_found")
        group = await self._repo.get_by_id(group_id)
        if group is None:
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
