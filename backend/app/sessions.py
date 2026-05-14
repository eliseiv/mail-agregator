"""Server-side session store backed by Redis (ADR-0004).

Two stores live here:

- :class:`SessionStore` — full user sessions (cookie ``mas_session``).
- :class:`SetupSessionStore` — short-lived password-setup sessions
  (cookie ``mas_setup``).

Key layout (``docs/05-modules.md`` sec. 3):

- ``session:{token}`` -> JSON
- ``user_sessions:{user_id}`` -> SET of tokens (TTL = absolute session TTL)
- ``setup_session:{token}`` -> JSON
"""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

import redis.asyncio as redis_asyncio

from shared.config import get_settings
from shared.logging import get_logger
from shared.redis_client import get_redis

log = get_logger(__name__)

SESSION_KEY_PREFIX = "session:"
USER_SESSIONS_KEY_PREFIX = "user_sessions:"
SETUP_SESSION_KEY_PREFIX = "setup_session:"


def _new_token() -> str:
    """32 random bytes, URL-safe base64 (no padding)."""
    return secrets.token_urlsafe(32)


def _ua_hash(user_agent: str | None) -> str:
    if not user_agent:
        return ""
    return hashlib.sha256(user_agent.encode("utf-8")).hexdigest()[:32]


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(slots=True)
class SessionData:
    """Cookie-session payload (Redis ``session:{token}`` JSON).

    Per ADR-0019 §10, ``role`` is the new three-valued enum
    (``super_admin`` / ``group_leader`` / ``group_member``) and
    ``group_id`` is the user's group (``None`` only for ``super_admin``).
    The legacy ``"admin"`` / ``"user"`` strings used before ADR-0019 are
    accepted on read for forward-compat with sessions created right
    before the migration; ``from_json`` upgrades them silently to the
    new vocabulary so a logged-in user does not get force-logged-out
    by the deploy. New sessions always serialise the new role.
    """

    user_id: int
    role: str  # "super_admin" | "group_leader" | "group_member"
    group_id: int | None
    csrf_token: str
    ip: str
    ua_hash: str
    created_at: str  # ISO
    last_seen_at: str  # ISO

    @classmethod
    def from_json(cls, raw: str) -> SessionData:
        d = json.loads(raw)
        # Legacy upgrade: pre-ADR-0019 sessions had no ``group_id`` and
        # used ``"admin"``/``"user"`` as the role. Map them so existing
        # cookies survive the deploy. Once all users have re-logged-in
        # this branch is dead code; safe to leave for the next release
        # cycle, then drop.
        legacy_role = d.get("role")
        if legacy_role == "admin":
            d["role"] = "super_admin"
        elif legacy_role == "user":
            d["role"] = "group_member"
        d.setdefault("group_id", None)
        return cls(**d)

    def to_json(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"))

    @property
    def is_super_admin(self) -> bool:
        return self.role == "super_admin"


@dataclass(slots=True)
class SetupSessionData:
    user_id: int
    csrf_token: str
    scope: str  # "set_password"
    created_at: str

    @classmethod
    def from_json(cls, raw: str) -> SetupSessionData:
        d = json.loads(raw)
        return cls(**d)

    def to_json(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"))


class SessionStore:
    """Full user sessions with sliding TTL (12 h default) and absolute cap (7 d)."""

    def __init__(self, client: redis_asyncio.Redis | None = None) -> None:
        self._r = client or get_redis()
        s = get_settings()
        self._ttl = s.SESSION_TTL_SECONDS
        self._abs_ttl = s.SESSION_ABSOLUTE_TTL_SECONDS

    async def create(
        self,
        user_id: int,
        role: str,
        group_id: int | None,
        ip: str,
        ua: str | None,
    ) -> tuple[str, str]:
        """Create a new session. Returns ``(session_token, csrf_token)``.

        Per ADR-0019 §10 the session payload now carries the three-valued
        ``role`` plus ``group_id`` so :class:`backend.app.deps.VisibilityScope`
        can be built without an extra DB lookup on every request.
        """
        if role not in {"super_admin", "group_leader", "group_member"}:
            raise ValueError(f"invalid role: {role!r}")
        # Invariant mirror (ADR-0019 §6 / 03-data-model.md): super_admin has
        # no group; non-admins must have one.
        if role == "super_admin" and group_id is not None:
            raise ValueError("super_admin must not have a group_id")
        if role != "super_admin" and group_id is None:
            raise ValueError(f"role={role!r} requires a non-null group_id")
        token = _new_token()
        csrf = _new_token()
        now = _now_iso()
        data = SessionData(
            user_id=user_id,
            role=role,
            group_id=group_id,
            csrf_token=csrf,
            ip=ip or "",
            ua_hash=_ua_hash(ua),
            created_at=now,
            last_seen_at=now,
        )
        async with self._r.pipeline(transaction=False) as pipe:
            pipe.set(SESSION_KEY_PREFIX + token, data.to_json(), ex=self._ttl)
            pipe.sadd(USER_SESSIONS_KEY_PREFIX + str(user_id), token)
            pipe.expire(USER_SESSIONS_KEY_PREFIX + str(user_id), self._abs_ttl)
            await pipe.execute()
        return token, csrf

    async def get(self, token: str) -> SessionData | None:
        if not token:
            return None
        raw = await self._r.get(SESSION_KEY_PREFIX + token)
        if raw is None:
            return None
        try:
            return SessionData.from_json(raw)
        except (json.JSONDecodeError, TypeError, KeyError):
            log.warning("session_corrupt_payload", token_prefix=token[:8])
            return None

    async def touch(self, token: str, data: SessionData) -> None:
        """Slide the TTL forward and update ``last_seen_at``."""
        data.last_seen_at = _now_iso()
        await self._r.set(SESSION_KEY_PREFIX + token, data.to_json(), ex=self._ttl)

    async def revoke(self, token: str) -> None:
        if not token:
            return
        data = await self.get(token)
        async with self._r.pipeline(transaction=False) as pipe:
            pipe.delete(SESSION_KEY_PREFIX + token)
            if data is not None:
                pipe.srem(USER_SESSIONS_KEY_PREFIX + str(data.user_id), token)
            await pipe.execute()

    async def revoke_all_for_user(self, user_id: int) -> int:
        """Force-logout every session of ``user_id``. Returns count deleted."""
        set_key = USER_SESSIONS_KEY_PREFIX + str(user_id)
        tokens = await self._r.smembers(set_key)  # type: ignore[misc]
        if not tokens:
            return 0
        async with self._r.pipeline(transaction=False) as pipe:
            for t in tokens:
                pipe.delete(SESSION_KEY_PREFIX + t)
            pipe.delete(set_key)
            await pipe.execute()
        return len(tokens)


class SetupSessionStore:
    """Short-lived setup session for first-login password setup (15 min)."""

    def __init__(self, client: redis_asyncio.Redis | None = None) -> None:
        self._r = client or get_redis()
        self._ttl = get_settings().SETUP_SESSION_TTL_SECONDS

    async def create(self, user_id: int) -> tuple[str, str]:
        token = _new_token()
        csrf = _new_token()
        data = SetupSessionData(
            user_id=user_id,
            csrf_token=csrf,
            scope="set_password",
            created_at=_now_iso(),
        )
        await self._r.set(SETUP_SESSION_KEY_PREFIX + token, data.to_json(), ex=self._ttl)
        return token, csrf

    async def get(self, token: str) -> SetupSessionData | None:
        if not token:
            return None
        raw = await self._r.get(SETUP_SESSION_KEY_PREFIX + token)
        if raw is None:
            return None
        try:
            return SetupSessionData.from_json(raw)
        except (json.JSONDecodeError, TypeError, KeyError):
            log.warning("setup_session_corrupt_payload", token_prefix=token[:8])
            return None

    async def revoke(self, token: str) -> None:
        if not token:
            return
        await self._r.delete(SETUP_SESSION_KEY_PREFIX + token)


def session_payload_for_logging(data: SessionData) -> dict[str, Any]:
    """Subset of session fields safe to log (no csrf, no ip)."""
    return {
        "user_id": data.user_id,
        "role": data.role,
        "created_at": data.created_at,
    }
