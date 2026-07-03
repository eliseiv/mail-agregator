"""Integration tests for :class:`ForwardingService` — CRUD + ACL (ADR-0034 §2).

Runs against the real test Postgres (``db_session`` rolls back per test). Covers
the authorisation matrix (leader / super_admin / group_member), e-mail
validation, the ``UNIQUE(group_id)`` "one row per team" invariant, delete, and
the ``forwarding_updated`` / ``forwarding_deleted`` audit rows.

Placed under ``tests/worker`` so CI's ``tests/unit|worker|frontend`` selection
gates it (see MEMORY ci-test-selection).
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.deps import VisibilityScope
from backend.app.exceptions import ForbiddenError, NotFoundError, ValidationError
from backend.app.forwarding.service import ForwardingService
from shared.models import AdminAudit, GroupForwarding
from shared.models.group import Group
from shared.models.user import (
    ROLE_GROUP_LEADER,
    ROLE_GROUP_MEMBER,
    ROLE_SUPER_ADMIN,
)

pytestmark = pytest.mark.integration  # needs DB

_GROUP_ID = 4100
_LEADER_UID = 4101


def _leader_scope(group_id: int = _GROUP_ID, user_id: int = _LEADER_UID) -> VisibilityScope:
    return VisibilityScope(
        user_id=user_id,
        role=ROLE_GROUP_LEADER,  # type: ignore[arg-type]
        group_id=group_id,
        group_ids=frozenset({group_id}),
    )


def _member_scope(group_id: int = _GROUP_ID) -> VisibilityScope:
    return VisibilityScope(
        user_id=4200,
        role=ROLE_GROUP_MEMBER,  # type: ignore[arg-type]
        group_id=group_id,
        group_ids=frozenset({group_id}),
    )


def _admin_scope() -> VisibilityScope:
    return VisibilityScope(
        user_id=1,
        role=ROLE_SUPER_ADMIN,  # type: ignore[arg-type]
        group_id=None,
        group_ids=frozenset(),
    )


async def _seed_group(session: AsyncSession, group_id: int = _GROUP_ID) -> None:
    session.add(Group(id=group_id, name=f"team-{group_id}", leader_user_id=None))
    await session.flush()


class TestUpsertLeader:
    async def test_create_then_update_single_row(self, db_session: AsyncSession) -> None:
        await _seed_group(db_session)
        svc = ForwardingService(db_session)

        dto, created = await svc.upsert_for_scope(
            _leader_scope(),
            forward_to="leader@company.com",
            is_active=None,
            override_group_id=None,
            ip="1.2.3.4",
            user_agent="pytest",
        )
        assert created is True
        assert dto.forward_to == "leader@company.com"
        assert dto.is_active is True  # default on create
        assert dto.group_id == _GROUP_ID

        # Second upsert updates the same row (idempotent PUT), created_at kept.
        dto2, created2 = await svc.upsert_for_scope(
            _leader_scope(),
            forward_to="new-leader@company.com",
            is_active=False,
            override_group_id=None,
            ip="1.2.3.4",
            user_agent="pytest",
        )
        assert created2 is False
        assert dto2.forward_to == "new-leader@company.com"
        assert dto2.is_active is False
        assert dto2.created_at == dto.created_at  # anchor never moves

        # UNIQUE(group_id): exactly one row for the team.
        count = await db_session.scalar(
            select(func.count())
            .select_from(GroupForwarding)
            .where(GroupForwarding.group_id == _GROUP_ID)
        )
        assert count == 1

    async def test_invalid_email_raises_validation_on_forward_to(
        self, db_session: AsyncSession
    ) -> None:
        await _seed_group(db_session)
        svc = ForwardingService(db_session)
        with pytest.raises(ValidationError) as ei:
            await svc.upsert_for_scope(
                _leader_scope(),
                forward_to="not-an-email",
                is_active=None,
                override_group_id=None,
                ip=None,
                user_agent=None,
            )
        assert ei.value.field == "forward_to"

    async def test_leader_passing_group_id_query_is_rejected(
        self, db_session: AsyncSession
    ) -> None:
        await _seed_group(db_session)
        svc = ForwardingService(db_session)
        with pytest.raises(ValidationError) as ei:
            await svc.upsert_for_scope(
                _leader_scope(),
                forward_to="leader@company.com",
                is_active=None,
                override_group_id=_GROUP_ID,  # leaders must NOT pass ?group_id
                ip=None,
                user_agent=None,
            )
        assert ei.value.field == "group_id"


class TestAcl:
    async def test_group_member_forbidden(self, db_session: AsyncSession) -> None:
        await _seed_group(db_session)
        svc = ForwardingService(db_session)
        with pytest.raises(ForbiddenError):
            await svc.upsert_for_scope(
                _member_scope(),
                forward_to="member@company.com",
                is_active=None,
                override_group_id=None,
                ip=None,
                user_agent=None,
            )

    async def test_super_admin_without_group_id_rejected(self, db_session: AsyncSession) -> None:
        svc = ForwardingService(db_session)
        with pytest.raises(ValidationError) as ei:
            await svc.upsert_for_scope(
                _admin_scope(),
                forward_to="admin@company.com",
                is_active=None,
                override_group_id=None,  # super_admin MUST pass ?group_id
                ip=None,
                user_agent=None,
            )
        assert ei.value.field == "group_id"

    async def test_super_admin_with_group_id_can_manage(self, db_session: AsyncSession) -> None:
        await _seed_group(db_session)
        svc = ForwardingService(db_session)
        dto, created = await svc.upsert_for_scope(
            _admin_scope(),
            forward_to="admin-set@company.com",
            is_active=None,
            override_group_id=_GROUP_ID,
            ip=None,
            user_agent=None,
        )
        assert created is True
        assert dto.forward_to == "admin-set@company.com"
        assert dto.group_id == _GROUP_ID


class TestReadAndDelete:
    async def test_get_missing_raises_not_found(self, db_session: AsyncSession) -> None:
        await _seed_group(db_session)
        svc = ForwardingService(db_session)
        with pytest.raises(NotFoundError):
            await svc.get_for_scope(_leader_scope(), override_group_id=None)

    async def test_delete_removes_row(self, db_session: AsyncSession) -> None:
        await _seed_group(db_session)
        svc = ForwardingService(db_session)
        await svc.upsert_for_scope(
            _leader_scope(),
            forward_to="leader@company.com",
            is_active=None,
            override_group_id=None,
            ip=None,
            user_agent=None,
        )
        await svc.delete_for_scope(
            _leader_scope(), override_group_id=None, ip=None, user_agent=None
        )
        count = await db_session.scalar(
            select(func.count())
            .select_from(GroupForwarding)
            .where(GroupForwarding.group_id == _GROUP_ID)
        )
        assert count == 0
        with pytest.raises(NotFoundError):
            await svc.get_for_scope(_leader_scope(), override_group_id=None)

    async def test_delete_missing_raises_not_found(self, db_session: AsyncSession) -> None:
        await _seed_group(db_session)
        svc = ForwardingService(db_session)
        with pytest.raises(NotFoundError):
            await svc.delete_for_scope(
                _leader_scope(), override_group_id=None, ip=None, user_agent=None
            )


class TestAudit:
    async def test_upsert_and_delete_write_audit_rows(self, db_session: AsyncSession) -> None:
        await _seed_group(db_session)
        svc = ForwardingService(db_session)
        await svc.upsert_for_scope(
            _leader_scope(),
            forward_to="leader@company.com",
            is_active=None,
            override_group_id=None,
            ip=None,
            user_agent=None,
        )
        await svc.delete_for_scope(
            _leader_scope(), override_group_id=None, ip=None, user_agent=None
        )
        actions = (
            (
                await db_session.execute(
                    select(AdminAudit.action)
                    .where(AdminAudit.actor_user_id == _LEADER_UID)
                    .order_by(AdminAudit.id)
                )
            )
            .scalars()
            .all()
        )
        assert "forwarding_updated" in actions
        assert "forwarding_deleted" in actions
