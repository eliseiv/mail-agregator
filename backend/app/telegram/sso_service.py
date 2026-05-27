"""Telegram Persistent SSO service (ADR-0022 §1).

Encapsulates the four interactions required by the auth flow:

- :meth:`verify_and_resolve` — HMAC-validate the initData, look up
  ``telegram_links`` and decide whether the caller already has a binding.
- :meth:`create_pending` — stash a one-shot Redis token referenced by
  the ``mas_tg_pending`` cookie (used when the SSO call lands without a
  binding and the user must complete an interactive login).
- :meth:`consume_pending` — read the ``mas_tg_pending`` cookie value back
  from Redis (called by :class:`AuthService` after a successful password
  verify); returns the ``telegram_user_id`` and deletes the Redis key.
- :meth:`link_pending` / :meth:`link_session_add` — bind a
  ``telegram_user_id`` to a ``user_id`` (login-flow vs authenticated
  session-add) applying the soft limit + rebind rules of ADR-0024 §3/§4.
- :meth:`revoke_for_user` — invoked from logout / admin reset /
  set-password flows; deletes **all** ``telegram_links`` rows of the user
  (ADR-0024 §5) and writes a single ``telegram_link_revoked`` audit entry.
- :meth:`revoke_one` — unlink one specific TG (``DELETE
  /api/telegram/links/{tg_user_id}``).

The service stores no in-memory state; the Redis token namespace is
``tg_pending:{token}``. All cryptographic decisions (HMAC, TTL) live in
:mod:`backend.app.telegram.init_data`.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Final, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.audit import AuditWriter
from backend.app.exceptions import (
    TelegramLinkLimitError,
    TelegramLinkOwnedByOtherError,
)
from backend.app.repositories.telegram_links import TelegramLinksRepo
from backend.app.telegram.init_data import (
    InitDataError,
    ValidatedInitData,
    verify_init_data,
)
from shared.config import get_settings
from shared.logging import get_logger
from shared.redis_client import get_redis

log = get_logger(__name__)

# Redis key namespaces — kept as module constants so other modules
# (tests, debug tools) reference them by symbol rather than free-form
# strings.
TG_PENDING_KEY_PREFIX: Final[str] = "tg_pending:"
TG_NOTIFY_QUEUE_KEY: Final[str] = "tg_notify_queue"


def _new_token() -> str:
    """32 random bytes, URL-safe base64 (no padding) — same shape as
    :func:`backend.app.sessions._new_token`. Reused for the pending-link
    cookie."""
    return secrets.token_urlsafe(32)


@dataclass(frozen=True, slots=True)
class SSOResolved:
    """Outcome of :meth:`TelegramSSOService.verify_and_resolve`.

    ``kind``:

    - ``"linked"`` — an active ``telegram_links`` row exists. Caller
      creates a full session for ``user_id`` and clears any pending cookie.
    - ``"unlinked"`` — initData is valid but no active link. Caller
      issues a pending-cookie via :meth:`create_pending` and redirects to
      ``/login``.
    """

    kind: Literal["linked", "unlinked"]
    telegram_user_id: int
    user_id: int | None
    validated: ValidatedInitData


class InvalidInitDataError(Exception):
    """Raised when :meth:`verify_and_resolve` cannot validate the payload.

    ``reason`` is an :data:`InitDataError` literal that the router maps to
    ``invalid_init_data`` / ``init_data_expired`` 401 envelopes.
    """

    def __init__(self, reason: InitDataError) -> None:
        super().__init__(reason)
        self.reason = reason


class TelegramSSOService:
    def __init__(self, session: AsyncSession) -> None:
        self._db = session
        self._links = TelegramLinksRepo(session)
        self._audit = AuditWriter(session)
        self._settings = get_settings()

    # --- validation + lookup ----------------------------------------------

    async def verify_and_resolve(self, init_data: str) -> SSOResolved:
        """Validate ``init_data`` and look up the active ``telegram_links``
        record. Raises :class:`InvalidInitDataError` on HMAC / expiry."""
        outcome = verify_init_data(
            init_data,
            bot_token=self._settings.BOT_TOKEN,
            max_age_seconds=self._settings.TG_AUTH_INIT_DATA_TTL_SECONDS,
        )
        if not isinstance(outcome, ValidatedInitData):
            raise InvalidInitDataError(outcome)

        link = await self._links.get_active_by_telegram_user_id(outcome.telegram_user_id)
        if link is not None:
            return SSOResolved(
                kind="linked",
                telegram_user_id=outcome.telegram_user_id,
                user_id=link.user_id,
                validated=outcome,
            )
        return SSOResolved(
            kind="unlinked",
            telegram_user_id=outcome.telegram_user_id,
            user_id=None,
            validated=outcome,
        )

    # --- pending token (mas_tg_pending cookie + Redis) --------------------

    async def create_pending(self, telegram_user_id: int) -> str:
        """Create the one-shot pending-link token. Returns the token to
        place into the ``mas_tg_pending`` cookie."""
        token = _new_token()
        redis = get_redis()
        await redis.set(
            TG_PENDING_KEY_PREFIX + token,
            str(telegram_user_id),
            ex=self._settings.TG_PENDING_LINK_TTL_SECONDS,
        )
        return token

    async def consume_pending(self, token: str) -> int | None:
        """Read the ``telegram_user_id`` previously stored under ``token``
        and delete the Redis key. Returns ``None`` if the token is missing
        / expired (the caller treats this as "no pending link")."""
        if not token:
            return None
        redis = get_redis()
        key = TG_PENDING_KEY_PREFIX + token
        async with redis.pipeline(transaction=False) as pipe:
            pipe.get(key)
            pipe.delete(key)
            results = await pipe.execute()
        raw = results[0]
        if raw is None:
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            log.warning("tg_pending_corrupt_value", token_prefix=token[:8])
            return None

    # --- linking ----------------------------------------------------------

    async def link_pending(
        self,
        *,
        telegram_user_id: int,
        user_id: int,
        ip: str,
        user_agent: str | None,
    ) -> None:
        """Bind ``telegram_user_id`` to ``user_id`` via the login-flow
        (pending-cookie redeemed after a successful password verify).

        ADR-0024 §3 replaces the old ``one user — one TG`` collision logic
        with a soft-limit + rebind model. Because this path carries a
        successful password proof, re-binding a TG owned by *another* user is
        allowed (the upsert ON CONFLICT (telegram_user_id) moves it). This
        method never raises — at the limit it audits and silently no-ops
        (the login itself still succeeds).
        """
        await self._link(
            telegram_user_id=telegram_user_id,
            user_id=user_id,
            ip=ip,
            user_agent=user_agent,
            allow_rebind_from_other=True,
            via="login_flow",
        )

    async def link_session_add(
        self,
        *,
        telegram_user_id: int,
        user_id: int,
        ip: str,
        user_agent: str | None,
    ) -> None:
        """Bind ``telegram_user_id`` to the already-authenticated ``user_id``
        (``POST /api/telegram/links`` — ADR-0024 §4).

        Unlike :meth:`link_pending` there is no password proof for the *other*
        owner, so re-binding a TG already linked to a different internal user
        is refused with :class:`TelegramLinkOwnedByOtherError`. At the soft
        limit raises :class:`TelegramLinkLimitError`.
        """
        await self._link(
            telegram_user_id=telegram_user_id,
            user_id=user_id,
            ip=ip,
            user_agent=user_agent,
            allow_rebind_from_other=False,
            via="session_add",
        )

    async def _link(
        self,
        *,
        telegram_user_id: int,
        user_id: int,
        ip: str,
        user_agent: str | None,
        allow_rebind_from_other: bool,
        via: str,
    ) -> None:
        """Shared link logic for both entry points (ADR-0024 §3/§4).

        Decision table:

        - existing link points at **another** user:
          - ``allow_rebind_from_other`` (login-flow) → rebind via upsert,
            audit ``telegram_link_rebound``;
          - else (session-add) → raise
            :class:`TelegramLinkOwnedByOtherError`.
        - existing link points at **this** user → refresh via upsert, audit
          ``telegram_link_created`` with ``replaced=true``.
        - no existing link → enforce ``COUNT(active) <
          TG_MAX_LINKS_PER_USER``; at the cap audit
          ``telegram_link_limit_reached`` and (login-flow) no-op or
          (session-add) raise :class:`TelegramLinkLimitError`; otherwise
          create + audit ``telegram_link_created``.
        """
        existing = await self._links.get_by_telegram_user_id(telegram_user_id)

        if existing is not None and existing.user_id != user_id:
            if not allow_rebind_from_other:
                raise TelegramLinkOwnedByOtherError(
                    "This Telegram account is linked to another user"
                )
            await self._links.upsert(telegram_user_id=telegram_user_id, user_id=user_id)
            await self._audit.log(
                actor_user_id=user_id,
                action="telegram_link_rebound",
                target_user_id=user_id,
                details={
                    "telegram_user_id": telegram_user_id,
                    "previous_user_id": existing.user_id,
                    "via": via,
                },
                ip=ip,
                user_agent=user_agent,
            )
            log.info(
                "telegram_link_rebound",
                user_id=user_id,
                telegram_user_id=telegram_user_id,
                previous_user_id=existing.user_id,
                via=via,
            )
            return

        if existing is not None and existing.user_id == user_id:
            # Same owner — refresh (clears dead_at, bumps created_at).
            await self._links.upsert(telegram_user_id=telegram_user_id, user_id=user_id)
            await self._audit.log(
                actor_user_id=user_id,
                action="telegram_link_created",
                target_user_id=user_id,
                details={"telegram_user_id": telegram_user_id, "replaced": True, "via": via},
                ip=ip,
                user_agent=user_agent,
            )
            log.info(
                "telegram_link_created",
                user_id=user_id,
                telegram_user_id=telegram_user_id,
                replaced=True,
                via=via,
            )
            return

        # New link — enforce the soft limit (ADR-0024 §3).
        active = await self._links.count_active_by_user_id(user_id)
        if active >= self._settings.TG_MAX_LINKS_PER_USER:
            await self._audit.log(
                actor_user_id=user_id,
                action="telegram_link_limit_reached",
                target_user_id=user_id,
                details={
                    "telegram_user_id": telegram_user_id,
                    "active_links": active,
                    "limit": self._settings.TG_MAX_LINKS_PER_USER,
                    "via": via,
                },
                ip=ip,
                user_agent=user_agent,
            )
            log.info(
                "telegram_link_limit_reached",
                user_id=user_id,
                telegram_user_id=telegram_user_id,
                active_links=active,
                limit=self._settings.TG_MAX_LINKS_PER_USER,
                via=via,
            )
            if not allow_rebind_from_other:
                raise TelegramLinkLimitError("Maximum number of Telegram links reached")
            return

        await self._links.upsert(telegram_user_id=telegram_user_id, user_id=user_id)
        await self._audit.log(
            actor_user_id=user_id,
            action="telegram_link_created",
            target_user_id=user_id,
            details={"telegram_user_id": telegram_user_id, "replaced": False, "via": via},
            ip=ip,
            user_agent=user_agent,
        )
        log.info(
            "telegram_link_created",
            user_id=user_id,
            telegram_user_id=telegram_user_id,
            replaced=False,
            via=via,
        )

    # --- revoke -----------------------------------------------------------

    async def revoke_for_user(
        self,
        *,
        user_id: int,
        reason: str,
        ip: str,
        user_agent: str | None,
    ) -> None:
        """Delete **all** links for ``user_id`` and audit (ADR-0024 §5).

        Used by logout / admin reset / stale-link cleanup. Writes a single
        ``telegram_link_revoked`` entry with ``details.telegram_user_ids`` =
        the list of removed chats. ``reason`` is canonical: ``"logout"``,
        ``"password_reset"``, ``"link_user_missing"``.
        """
        deleted = await self._links.delete_all_by_user_id(user_id)
        if not deleted:
            return
        await self._audit.log(
            actor_user_id=user_id,
            action="telegram_link_revoked",
            target_user_id=user_id,
            details={
                "telegram_user_ids": deleted,
                "reason": reason,
            },
            ip=ip,
            user_agent=user_agent,
        )
        log.info(
            "telegram_link_revoked",
            user_id=user_id,
            telegram_user_ids=deleted,
            reason=reason,
        )

    async def revoke_one(
        self,
        *,
        user_id: int,
        telegram_user_id: int,
        ip: str,
        user_agent: str | None,
    ) -> bool:
        """Unlink one specific TG owned by ``user_id`` (ADR-0024 §4 —
        ``DELETE /api/telegram/links/{tg_user_id}``).

        Returns ``True`` iff a row was deleted. Idempotent — a missing row
        (already unlinked / never owned) returns ``False`` without auditing.
        """
        deleted = await self._links.delete_one(user_id=user_id, telegram_user_id=telegram_user_id)
        if not deleted:
            return False
        await self._audit.log(
            actor_user_id=user_id,
            action="telegram_link_revoked",
            target_user_id=user_id,
            details={
                "telegram_user_id": telegram_user_id,
                "reason": "user_unlink",
            },
            ip=ip,
            user_agent=user_agent,
        )
        log.info(
            "telegram_link_revoked",
            user_id=user_id,
            telegram_user_id=telegram_user_id,
            reason="user_unlink",
        )
        return True

    # --- dead-link marker --------------------------------------------------

    async def mark_link_dead(
        self,
        *,
        telegram_user_id: int,
        user_id: int,
        reason: str,
    ) -> None:
        """Mark a link as dead (Bot API 403/400). Writes an audit entry
        with ``telegram_link_dead_marked``."""
        await self._links.mark_dead(telegram_user_id)
        await self._audit.log(
            actor_user_id=user_id,
            action="telegram_link_dead_marked",
            target_user_id=user_id,
            details={
                "telegram_user_id": telegram_user_id,
                "reason": reason,
            },
        )
        log.info(
            "telegram_link_dead_marked",
            user_id=user_id,
            telegram_user_id=telegram_user_id,
            reason=reason,
        )
