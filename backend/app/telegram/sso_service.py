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
- :meth:`link_pending` — perform the atomic upsert in ``telegram_links``
  for the resolved ``telegram_user_id`` / ``user_id`` pair plus audit.
- :meth:`revoke_for_user` — invoked from logout / admin reset /
  set-password flows; deletes the ``telegram_links`` row and writes a
  ``telegram_link_revoked`` audit entry.

The service stores no in-memory state; the Redis token namespace is
``tg_pending:{token}``. All cryptographic decisions (HMAC, TTL) live in
:mod:`backend.app.telegram.init_data`.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Final, Literal

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.audit import AuditWriter
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
        """Upsert ``telegram_links`` for ``(telegram_user_id, user_id)``.

        On UNIQUE conflict on ``user_id`` (another Telegram account is
        already linked to this internal user — ADR-0022 §1.4 ``one user —
        one tg`` invariant), we write a ``telegram_link_collision`` audit
        entry and **silently skip** the upsert. The user can resolve the
        collision by logging out in the other Telegram client first.
        """
        # Pre-check for UNIQUE(user_id) collision: another telegram_user_id
        # may already be linked to this internal user.
        existing_for_user = await self._links.get_by_user_id(user_id)
        if existing_for_user is not None and existing_for_user.telegram_user_id != telegram_user_id:
            await self._audit.log(
                actor_user_id=user_id,
                action="telegram_link_collision",
                target_user_id=user_id,
                details={
                    "existing_telegram_user_id": existing_for_user.telegram_user_id,
                    "attempted_telegram_user_id": telegram_user_id,
                },
                ip=ip,
                user_agent=user_agent,
            )
            log.info(
                "telegram_link_collision",
                user_id=user_id,
                attempted_telegram_user_id=telegram_user_id,
                existing_telegram_user_id=existing_for_user.telegram_user_id,
            )
            return

        try:
            _row, replaced = await self._links.upsert(
                telegram_user_id=telegram_user_id, user_id=user_id
            )
        except IntegrityError:
            # Defence-in-depth: race between the pre-check above and the
            # upsert (a concurrent link from another tg user to the same
            # internal user). Treat as collision.
            await self._audit.log(
                actor_user_id=user_id,
                action="telegram_link_collision",
                target_user_id=user_id,
                details={
                    "attempted_telegram_user_id": telegram_user_id,
                    "reason": "unique_user_id_race",
                },
                ip=ip,
                user_agent=user_agent,
            )
            return

        await self._audit.log(
            actor_user_id=user_id,
            action="telegram_link_created",
            target_user_id=user_id,
            details={
                "telegram_user_id": telegram_user_id,
                "replaced": replaced,
            },
            ip=ip,
            user_agent=user_agent,
        )
        log.info(
            "telegram_link_created",
            user_id=user_id,
            telegram_user_id=telegram_user_id,
            replaced=replaced,
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
        """Delete the link for ``user_id`` and audit.

        ``reason`` ends up in ``details.reason`` of the audit entry; canonical
        values: ``"logout"``, ``"password_reset"``.
        """
        deleted = await self._links.delete_by_user_id(user_id)
        if deleted is None:
            return
        await self._audit.log(
            actor_user_id=user_id,
            action="telegram_link_revoked",
            target_user_id=user_id,
            details={
                "telegram_user_id": deleted.telegram_user_id,
                "reason": reason,
            },
            ip=ip,
            user_agent=user_agent,
        )
        log.info(
            "telegram_link_revoked",
            user_id=user_id,
            telegram_user_id=deleted.telegram_user_id,
            reason=reason,
        )

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
