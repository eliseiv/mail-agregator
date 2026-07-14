"""Seed of the ``crm-service`` technical user (ADR-0039 §Q-0039-1).

ADR-0044 §4 (phase A3): the session ``AuthService`` (two-step login, lockout,
set-password, login audit) and ``seed_super_admin`` went away with the cookie
UI — the connector has no interactive users. This module survives ONLY as the
source of :data:`CRM_SERVICE_USERNAME` + :func:`seed_crm_service_user` (owner
of every mailbox; used by the API lifespan and
``backend/app/external/write_service.py``).
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.repositories.users import UsersRepo
from shared.logging import get_logger
from shared.models import ROLE_SUPER_ADMIN

log = get_logger(__name__)


# --- crm-service technical user seed (ADR-0039 §Q-0039-1) -------------------

# Username of the headless-CRM technical owner of externally-created mailboxes.
# Role ``super_admin``, no login password. Lowercase to satisfy
# ``ck_users_username_lower``.
CRM_SERVICE_USERNAME = "crm-service"


async def seed_crm_service_user(session: AsyncSession) -> str:
    """Idempotent seed of the ``crm-service`` technical user (ADR-0039 §Q-0039-1).

    Owner of ALL mailboxes (ADR-0043 §4 — no other owners exist after the
    decommission). Seeded on API startup. Returns ``"created"`` | ``"updated"``
    | ``"unchanged"``.

    No login password (``password_hash=NULL``): interactive login does not exist
    in the connector.
    """
    repo = UsersRepo(session)
    existing = await repo.get_by_username(CRM_SERVICE_USERNAME)
    if existing is not None:
        # Defensive: keep the role invariant even if the row was tampered with.
        # Never sets a login password.
        if existing.role != ROLE_SUPER_ADMIN:
            existing.role = ROLE_SUPER_ADMIN
            existing.updated_at = datetime.now(UTC)
            await session.flush()
            log.info("crm_service_seed_updated")
            return "updated"
        log.info("crm_service_seed_unchanged")
        return "unchanged"

    # Race-safety: two processes starting together both see ``existing=None``
    # (neither has committed), both INSERT, and the loser hits the ``username``
    # UNIQUE. Run the INSERT in a SAVEPOINT so an ``IntegrityError`` rolls back
    # ONLY the nested block; on conflict the winner's row is already there.
    try:
        async with session.begin_nested():
            await repo.create(
                username=CRM_SERVICE_USERNAME,
                email=None,
                role=ROLE_SUPER_ADMIN,
                display_name=None,
                password_hash=None,
                password_reset_required=False,
                password_encrypted=None,
            )
    except IntegrityError:
        # Another process won the race — the row now exists.
        log.info("crm_service_seed_race_skipped")
        return "unchanged"
    log.info("crm_service_seed_created")
    return "created"
