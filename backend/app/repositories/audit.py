"""Repository for ``admin_audit``."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models import AdminAudit


class AuditRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def insert(
        self,
        *,
        actor_user_id: int,
        action: str,
        target_user_id: int | None = None,
        target_username: str | None = None,
        details: dict[str, Any] | None = None,
        ip: str | None = None,
        user_agent: str | None = None,
    ) -> AdminAudit:
        # Truncate UA to 256 chars per data-model contract.
        if user_agent:
            user_agent = user_agent[:256]
        record = AdminAudit(
            actor_user_id=actor_user_id,
            action=action,
            target_user_id=target_user_id,
            target_username=target_username,
            details=details,
            ip=ip,
            user_agent=user_agent,
        )
        self._s.add(record)
        await self._s.flush()
        await self._s.refresh(record)
        return record

    async def list_paged(
        self,
        *,
        action: str | None,
        target_user_id: int | None,
        from_date: datetime | None,
        to_date: datetime | None,
        page: int,
        limit: int,
    ) -> tuple[list[AdminAudit], int]:
        stmt = select(AdminAudit).order_by(AdminAudit.created_at.desc())
        count_stmt = select(func.count()).select_from(AdminAudit)
        if action:
            stmt = stmt.where(AdminAudit.action == action)
            count_stmt = count_stmt.where(AdminAudit.action == action)
        if target_user_id is not None:
            stmt = stmt.where(AdminAudit.target_user_id == target_user_id)
            count_stmt = count_stmt.where(AdminAudit.target_user_id == target_user_id)
        if from_date is not None:
            stmt = stmt.where(AdminAudit.created_at >= from_date)
            count_stmt = count_stmt.where(AdminAudit.created_at >= from_date)
        if to_date is not None:
            stmt = stmt.where(AdminAudit.created_at <= to_date)
            count_stmt = count_stmt.where(AdminAudit.created_at <= to_date)
        total = (await self._s.execute(count_stmt)).scalar_one()
        page = max(page, 1)
        limit = max(min(limit, 200), 1)
        stmt = stmt.offset((page - 1) * limit).limit(limit)
        items = list((await self._s.execute(stmt)).scalars().all())
        return items, int(total)
