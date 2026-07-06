"""AuditWriter — wraps :class:`AuditRepo` so callers don't reach into ORM.

Per ``docs/05-modules.md`` sec. 12: an audit-write failure must propagate so
the calling business operation can roll back. We intentionally do not swallow.
"""

from __future__ import annotations

from typing import Any, Final

from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.repositories.audit import AuditRepo

# Closed enum from ``docs/03-data-model.md`` table ``admin_audit`` +
# ADR-0019 §9 (group / role lifecycle actions) +
# ADR-0022 §1.4 (Telegram SSO + notification lifecycle) +
# ADR-0023 §G (outbound-webhook lifecycle).
ALLOWED_ACTIONS: Final[frozenset[str]] = frozenset(
    {
        "admin_login",
        "admin_logout",
        "create_user",
        "reset_password",
        "delete_user",
        "lockout_triggered",
        "account_auto_disabled",
        # ADR-0019 §9 — group / role lifecycle.
        "group_create",
        "group_delete",
        "group_rename",
        "user_role_change",
        "user_group_change",
        # ADR-0030 — multi-group membership add/remove (additional memberships).
        "user_group_add",
        "user_group_remove",
        # ADR-0022 §1.4 — Telegram SSO + notification lifecycle.
        "telegram_link_created",
        "telegram_link_revoked",
        "telegram_link_dead_marked",
        # ADR-0024 — multi-TG link lifecycle.
        "telegram_link_rebound",
        "telegram_link_limit_reached",
        # deprecated (ADR-0024 §3): no longer written, kept for historical rows.
        "telegram_link_collision",
        # ADR-0023 §G — outbound webhook lifecycle.
        "webhook_created",
        "webhook_updated",
        "webhook_deleted",
        "webhook_secret_rotated",
        "webhook_dead_marked",
        # ADR-0025 §8 — OAuth Outlook account lifecycle.
        "oauth_account_linked",
        "oauth_refresh_invalidated",
        # ADR-0026 §3 — sync circuit-breaker mass-failure suppression.
        "sync_mass_failure_suppressed",
        # ADR-0031 §6 — mailbox team transfer (PATCH group_id).
        "mail_account_group_change",
        # ADR-0034 §2 — mail-forwarding config upsert/delete.
        "forwarding_updated",
        "forwarding_deleted",
        # ADR-0038 §3/§4 — reversible login-password lifecycle.
        "user_password_set",
        "user_password_revealed",
    }
)


class AuditWriter:
    def __init__(self, session: AsyncSession) -> None:
        self._repo = AuditRepo(session)

    async def log(
        self,
        *,
        actor_user_id: int,
        action: str,
        target_user_id: int | None = None,
        target_username: str | None = None,
        details: dict[str, Any] | None = None,
        ip: str | None = None,
        user_agent: str | None = None,
    ) -> None:
        if action not in ALLOWED_ACTIONS:
            raise ValueError(f"Audit action not in enum: {action!r}")
        await self._repo.insert(
            actor_user_id=actor_user_id,
            action=action,
            target_user_id=target_user_id,
            target_username=target_username,
            details=details,
            ip=ip,
            user_agent=user_agent,
        )
