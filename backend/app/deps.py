"""FastAPI dependency callables (post-ADR-0044, headless connector).

Exactly what the surviving ``external`` / ``health`` routers need (ADR-0044 §4,
phase A1 — the by-name keep-list):

- :func:`get_db` / :data:`DbSession` — the :class:`AsyncSession` source;
- :class:`VisibilityScope` — the dataclass the external write path builds
  SYNTHETICALLY for the ``crm-service`` technical user (``write_service.py``,
  a super_admin scope) rather than through any session machinery.

Removed (phase A1): ``current_session`` / ``current_user`` / ``build_scope`` /
``current_scope`` / ``require_super_admin`` / ``require_admin_or_leader`` /
``require_admin`` / ``get_session_token`` / ``is_form_request`` /
``assert_owns`` — all of them leaned on the cookie session and the HTML UI,
neither of which exists after the decommission.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated, Literal

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db import get_session
from shared.models import ROLE_GROUP_LEADER, ROLE_GROUP_MEMBER, ROLE_SUPER_ADMIN

Role = Literal["super_admin", "group_leader", "group_member"]


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency for an :class:`AsyncSession`."""
    async for session in get_session():
        yield session


DbSession = Annotated[AsyncSession, Depends(get_db)]


# ---------------------------------------------------------------------------
# VisibilityScope (ADR-0019 §7 — kept as the service-layer contract)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class VisibilityScope:
    """Caller-relative authorisation scope.

    After ADR-0044 the only source of a scope is the synthetic ``crm-service``
    super_admin (``backend/app/external/write_service.py``): the aggregator has
    no interactive sessions or roles left. The dataclass survives because the
    reused :class:`backend.app.accounts.service.MailAccountService` is typed
    against it.
    """

    user_id: int
    role: Role
    group_id: int | None  # always None — teams live in the CRM only
    group_ids: frozenset[int]

    @property
    def is_super_admin(self) -> bool:
        return self.role == ROLE_SUPER_ADMIN

    @property
    def is_group_leader(self) -> bool:
        return self.role == ROLE_GROUP_LEADER

    @property
    def is_group_member(self) -> bool:
        return self.role == ROLE_GROUP_MEMBER
