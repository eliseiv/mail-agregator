"""ForwardingService — CRUD (upsert) for the per-team forwarding config
(ADR-0034 §2).

Fork of :class:`backend.app.webhooks.service.WebhooksService` **without** a
secret. All public methods accept the caller's :class:`VisibilityScope` and
enforce authorisation themselves:

- ``group_leader`` → own group only (no ``?group_id`` query);
- ``super_admin``  → any group via a mandatory ``?group_id=<int>``;
- ``group_member`` → 403.

The service does **not** open transactions — the router wraps every mutating
call in ``async with db.begin():`` so the audit write commits atomically with
the business row (symmetric to :class:`WebhooksService`).
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.audit import AuditWriter
from backend.app.deps import VisibilityScope
from backend.app.exceptions import (
    ForbiddenError,
    NotFoundError,
    ValidationError,
)
from backend.app.forwarding.schemas import (
    ForwardingDTO,
    ForwardToValidationError,
    validate_forward_to,
)
from backend.app.repositories.group_forwarding import GroupForwardingRepo
from shared.logging import get_logger
from shared.models import GroupForwarding

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class _ResolvedTarget:
    """Result of :meth:`ForwardingService._resolve_target_group_id`."""

    group_id: int
    target_user_id: int | None  # leader of the resolved group (for audit)


def _to_dto(row: GroupForwarding) -> ForwardingDTO:
    return ForwardingDTO(
        id=row.id,
        group_id=row.group_id,
        forward_to=row.forward_to,
        is_active=row.is_active,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class ForwardingService:
    def __init__(self, session: AsyncSession) -> None:
        self._db = session
        self._repo = GroupForwardingRepo(session)
        self._audit = AuditWriter(session)

    # --- Authorisation helper --------------------------------------------

    def _resolve_target_group_id(
        self, scope: VisibilityScope, *, override_group_id: int | None
    ) -> _ResolvedTarget:
        """Decide which group_id this call acts on (ADR-0034 §2, copy of
        ``WebhooksService._resolve_target_group_id``).

        - ``group_member`` → 403;
        - ``super_admin`` MUST pass ``override_group_id`` (else 400);
        - ``group_leader`` MUST NOT pass ``override_group_id`` (else 400);
          acts on ``scope.group_id``.
        """
        if scope.is_group_member:
            raise ForbiddenError("group members cannot manage forwarding")

        if scope.is_super_admin:
            if override_group_id is None:
                raise ValidationError(
                    "super_admin must pass ?group_id=<int>",
                    field="group_id",
                )
            return _ResolvedTarget(group_id=override_group_id, target_user_id=None)

        # group_leader path.
        if override_group_id is not None:
            raise ValidationError(
                "group_leader cannot pass ?group_id=<int>",
                field="group_id",
            )
        if scope.group_id is None:
            # Defensive — a leader without a group is a data-model
            # inconsistency; 404 is correct (nothing to manage).
            raise NotFoundError("caller has no group")
        return _ResolvedTarget(group_id=scope.group_id, target_user_id=scope.user_id)

    # --- Reads ------------------------------------------------------------

    async def get_for_scope(
        self, scope: VisibilityScope, *, override_group_id: int | None
    ) -> ForwardingDTO:
        target = self._resolve_target_group_id(scope, override_group_id=override_group_id)
        row = await self._repo.get_by_group_id(target.group_id)
        if row is None:
            raise NotFoundError("forwarding is not configured for this group")
        return _to_dto(row)

    # --- Writes -----------------------------------------------------------

    async def upsert_for_scope(
        self,
        scope: VisibilityScope,
        *,
        forward_to: str | None,
        is_active: bool | None,
        override_group_id: int | None,
        ip: str | None,
        user_agent: str | None,
    ) -> tuple[ForwardingDTO, bool]:
        """Create or update the team's forwarding config (idempotent PUT).

        Returns ``(dto, created)`` — ``created`` is ``True`` when a new row was
        inserted (HTTP 201) and ``False`` when an existing row was updated
        (HTTP 200). ``created_at`` is never modified on update (it anchors the
        "don't flood history" filter, ADR-0034 §3.4).
        """
        target = self._resolve_target_group_id(scope, override_group_id=override_group_id)
        try:
            forward_to_clean = validate_forward_to(forward_to)
        except ForwardToValidationError as exc:
            raise ValidationError(str(exc), field="forward_to") from exc

        existing = await self._repo.get_by_group_id(target.group_id)
        if existing is None:
            effective_active = True if is_active is None else is_active
            row = await self._repo.insert(
                group_id=target.group_id,
                forward_to=forward_to_clean,
                is_active=effective_active,
            )
            created = True
        else:
            fields: dict[str, object] = {"forward_to": forward_to_clean}
            if is_active is not None:
                fields["is_active"] = is_active
            await self._repo.update_fields(existing.id, **fields)
            refreshed = await self._repo.get_by_id(existing.id)
            if refreshed is None:
                # Concurrent delete — surface 404 so the caller refetches.
                raise NotFoundError("forwarding was removed concurrently")
            row = refreshed
            created = False

        await self._audit.log(
            actor_user_id=scope.user_id,
            action="forwarding_updated",
            target_user_id=target.target_user_id,
            details={
                "group_id": row.group_id,
                "forward_to": row.forward_to,
                "is_active": row.is_active,
            },
            ip=ip,
            user_agent=user_agent,
        )
        log.info(
            "forwarding_updated",
            group_id=row.group_id,
            actor_user_id=scope.user_id,
            created=created,
        )
        return _to_dto(row), created

    async def delete_for_scope(
        self,
        scope: VisibilityScope,
        *,
        override_group_id: int | None,
        ip: str | None,
        user_agent: str | None,
    ) -> None:
        target = self._resolve_target_group_id(scope, override_group_id=override_group_id)
        row = await self._repo.get_by_group_id(target.group_id)
        if row is None:
            raise NotFoundError("forwarding is not configured for this group")

        group_id = row.group_id
        await self._repo.delete(row.id)

        await self._audit.log(
            actor_user_id=scope.user_id,
            action="forwarding_deleted",
            target_user_id=target.target_user_id,
            details={"group_id": group_id},
            ip=ip,
            user_agent=user_agent,
        )
        log.info(
            "forwarding_deleted",
            group_id=group_id,
            actor_user_id=scope.user_id,
        )
