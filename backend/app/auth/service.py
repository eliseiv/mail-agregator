"""AuthService — login, set-password, logout, super-admin seed.

Implements ``docs/05-modules.md`` sec. 7. State machine in
``Состояния пользователя при логине`` mermaid diagram.

Anti-timing: when the username is unknown we still run argon2 verify against
a fixed dummy hash so attackers can't distinguish "user exists" from "user
does not exist" by response time.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.audit import AuditWriter
from backend.app.exceptions import (
    AccountLockedError,
    NotAuthenticatedError,
)
from backend.app.repositories.users import UsersRepo
from backend.app.sessions import SessionStore, SetupSessionStore
from shared.config import get_settings
from shared.logging import get_logger

log = get_logger(__name__)


# Fixed dummy hash for anti-timing comparison. Generated once at module load.
_PH = PasswordHasher()
_DUMMY_HASH = _PH.hash("anti-timing-placeholder-not-a-real-password")


@dataclass(slots=True)
class LoginResult:
    kind: Literal["session_created", "set_password_required", "invalid", "locked"]
    session_token: str | None = None
    setup_token: str | None = None
    csrf: str | None = None
    role: str | None = None
    user_id: int | None = None
    retry_after_sec: int | None = None
    is_admin: bool = False


@dataclass(slots=True)
class LoginLookupResult:
    """Result of step-1 of the two-step login flow (ADR-0016).

    ``kind``:

    - ``"not_found"`` — no such user. To avoid user-enumeration, the router
      forwards the browser to the password step anyway with the submitted
      username carried in the ``mas_login`` cookie; step-2 will then return
      a generic ``invalid_credentials`` response.
    - ``"set_password_required"`` — user exists and has
      ``password_reset_required=true``. ``setup_token`` is populated; the
      router redirects to ``/set-password``.
    - ``"ready_for_password"`` — user exists and has a password set. The
      router redirects to ``/login/password``.

    Note: this method does **not** apply per-user rate-limiting or write to
    audit. Both happen at step-2 once the password is actually verified —
    the same behaviour as before two-step.
    """

    kind: Literal["not_found", "set_password_required", "ready_for_password"]
    user_id: int | None = None
    setup_token: str | None = None


class AuthService:
    def __init__(self, session: AsyncSession) -> None:
        self._db = session
        self._users = UsersRepo(session)
        self._audit = AuditWriter(session)
        self._sessions = SessionStore()
        self._setup = SetupSessionStore()
        self._settings = get_settings()
        self._ph = _PH

    # --- Two-step login: step-1 (username only, ADR-0016) -----------------

    async def lookup_for_login(self, *, username: str) -> LoginLookupResult:
        """Resolve ``username`` -> next step of the two-step login flow.

        Behaviour matrix (per ADR-0016):

        ===================  ====================================
        Outcome              ``LoginLookupResult.kind``
        ===================  ====================================
        no user              ``"not_found"``
        password not set     ``"set_password_required"`` + setup
        password set         ``"ready_for_password"``
        ===================  ====================================

        We deliberately ignore lockout state here — lockout takes effect at
        step-2 (after the password is presented), matching the original
        single-step semantics. Surfacing lockout at step-1 would leak
        existence and timing information about which usernames are real.
        """
        user = await self._users.get_by_username(username)
        if user is None:
            return LoginLookupResult(kind="not_found")
        if user.password_reset_required or user.password_hash is None:
            setup_token, _csrf = await self._setup.create(user.id)
            return LoginLookupResult(
                kind="set_password_required",
                user_id=user.id,
                setup_token=setup_token,
            )
        return LoginLookupResult(kind="ready_for_password", user_id=user.id)

    # --- Login ------------------------------------------------------------

    async def login(
        self, *, username: str, password: str, ip: str, user_agent: str | None
    ) -> LoginResult:
        """Authenticate and either create a full session or a setup session.

        Per ADR-0009 lockout is checked **before** password verification but
        after a normalised username lookup; argon2 still runs (against the
        dummy hash) on unknown usernames for timing parity.
        """
        user = await self._users.get_by_username(username)

        # Lockout check first — even existing user with valid password is
        # rejected if locked.
        if user is not None and user.lockout_until is not None:
            now = datetime.now(UTC)
            if user.lockout_until > now:
                retry = int((user.lockout_until - now).total_seconds())
                return LoginResult(kind="locked", retry_after_sec=max(retry, 1))

        # Password reset path: ignore submitted password, emit setup-session.
        if user is not None and user.password_reset_required:
            setup_token, csrf = await self._setup.create(user.id)
            return LoginResult(
                kind="set_password_required",
                setup_token=setup_token,
                csrf=csrf,
                user_id=user.id,
            )

        # Normal verify path. Always call ph.verify for timing parity, even
        # when there is no user row (then we verify against the dummy hash
        # and ignore the result).
        password_hash = user.password_hash if user is not None else _DUMMY_HASH
        if password_hash is None:
            # Defensive: hash NULL but reset flag false (broken state). Treat
            # as set-password-required for the existing user.
            if user is not None:
                setup_token, csrf = await self._setup.create(user.id)
                return LoginResult(
                    kind="set_password_required",
                    setup_token=setup_token,
                    csrf=csrf,
                    user_id=user.id,
                )
            password_hash = _DUMMY_HASH

        verified = False
        try:
            self._ph.verify(password_hash, password)
            verified = True
        except (VerifyMismatchError, InvalidHashError):
            verified = False
        except Exception:  # — argon2 may raise low-level errors
            verified = False

        if not verified or user is None:
            if user is not None:
                attempts, lockout = await self._users.record_login_failure(
                    user.id,
                    threshold=self._settings.LOGIN_FAILURE_THRESHOLD,
                    lockout_minutes=self._settings.LOGIN_LOCKOUT_MINUTES,
                )
                if lockout is not None and attempts == self._settings.LOGIN_FAILURE_THRESHOLD:
                    # Just-triggered lockout — write audit so admins see it.
                    await self._audit.log(
                        actor_user_id=user.id,
                        action="lockout_triggered",
                        target_user_id=user.id,
                        target_username=user.username,
                        ip=ip,
                        user_agent=user_agent,
                    )
            return LoginResult(kind="invalid")

        # Re-hash if argon2 parameters changed.
        if self._ph.check_needs_rehash(password_hash):
            new_hash = self._ph.hash(password)
            await self._users.set_password_hash(user.id, new_hash)

        await self._users.record_login_success(user.id)
        role = "admin" if user.is_admin else "user"
        token, csrf = await self._sessions.create(user.id, role, ip, user_agent)

        if user.is_admin:
            await self._audit.log(
                actor_user_id=user.id,
                action="admin_login",
                ip=ip,
                user_agent=user_agent,
            )

        return LoginResult(
            kind="session_created",
            session_token=token,
            csrf=csrf,
            role=role,
            user_id=user.id,
            is_admin=user.is_admin,
        )

    # --- Set-password ------------------------------------------------------

    async def complete_set_password(
        self,
        *,
        setup_token: str,
        password: str,
        ip: str,
        user_agent: str | None,
    ) -> LoginResult:
        """Validate the setup-session, hash and persist the new password,
        revoke the setup-session, create a full session.
        """
        setup = await self._setup.get(setup_token)
        if setup is None or setup.scope != "set_password":
            raise NotAuthenticatedError("Setup session expired")

        user = await self._users.get_by_id(setup.user_id)
        if user is None:
            raise NotAuthenticatedError("User no longer exists")

        new_hash = self._ph.hash(password)
        await self._users.set_password_hash(user.id, new_hash)
        await self._setup.revoke(setup_token)

        role = "admin" if user.is_admin else "user"
        session_token, csrf = await self._sessions.create(user.id, role, ip, user_agent)

        if user.is_admin:
            # Edge: super-admin path normally goes through seed_super_admin
            # which sets password_reset_required=false, so this branch only
            # fires for an admin that was hand-reset. Still log to audit.
            await self._audit.log(
                actor_user_id=user.id,
                action="admin_login",
                ip=ip,
                user_agent=user_agent,
            )

        return LoginResult(
            kind="session_created",
            session_token=session_token,
            csrf=csrf,
            role=role,
            user_id=user.id,
            is_admin=user.is_admin,
        )

    # --- Logout ------------------------------------------------------------

    async def logout(
        self,
        *,
        session_token: str,
        actor_user_id: int,
        is_admin: bool,
        ip: str,
        user_agent: str | None,
    ) -> None:
        if is_admin:
            await self._audit.log(
                actor_user_id=actor_user_id,
                action="admin_logout",
                ip=ip,
                user_agent=user_agent,
            )
        await self._sessions.revoke(session_token)

    # --- Lockout helpers ---------------------------------------------------

    @staticmethod
    def is_currently_locked(lockout_until: datetime | None) -> int | None:
        """Return seconds-until-unlock (>=1) or None if not locked."""
        if lockout_until is None:
            return None
        now = datetime.now(UTC)
        if lockout_until <= now:
            return None
        return max(int((lockout_until - now).total_seconds()), 1)


# --- Super-admin seed -------------------------------------------------------


async def seed_super_admin(session: AsyncSession) -> str:
    """Idempotent UPSERT of the super-admin from env (``docs/05-modules.md`` sec. 7).

    Returns one of: ``"created"`` | ``"updated"`` | ``"unchanged"``.
    Always logs a structured event.

    Note on the ``email`` field: the super-admin record's ``email`` is NOT
    sourced from env — by design there is no ``ADMIN_EMAIL`` variable. The
    operator can populate it via the admin UI later. This means the
    ``"unchanged"`` fast-path here intentionally does not check ``email``
    (and the slow-path UPSERT in :meth:`UsersRepo.upsert_admin` also does
    not overwrite it) — env-driven seeding only owns ``username``,
    ``password_hash``, ``is_admin``, ``password_reset_required``,
    ``failed_login_attempts``, and ``lockout_until``.

    Username is normalised to lower-case via :meth:`UsersRepo.upsert_admin`
    and :meth:`UsersRepo.get_by_username` (defence-in-depth: migration
    ``20260505_002`` adds a CHECK constraint), so an ``ADMIN_LOGIN=Admin``
    env value is treated identically to ``admin``.
    """
    settings = get_settings()
    repo = UsersRepo(session)

    new_hash = _PH.hash(settings.ADMIN_PASSWORD)

    existing = await repo.get_by_username(settings.ADMIN_LOGIN)
    if existing is not None and existing.password_hash:
        try:
            _PH.verify(existing.password_hash, settings.ADMIN_PASSWORD)
            same_password = True
        except (VerifyMismatchError, InvalidHashError):
            same_password = False
        if same_password:
            # Still ensure flags are correct in case admin row was tampered.
            if (
                existing.is_admin
                and not existing.password_reset_required
                and existing.lockout_until is None
                and existing.failed_login_attempts == 0
            ):
                log.info("admin_seed_unchanged", username=settings.ADMIN_LOGIN)
                return "unchanged"

    _, status = await repo.upsert_admin(username=settings.ADMIN_LOGIN, password_hash=new_hash)
    if status == "created":
        log.info("admin_seed_created", username=settings.ADMIN_LOGIN)
    else:
        log.info("admin_seed_password_updated", username=settings.ADMIN_LOGIN)
    return status


# --- Helper to surface AccountLockedError uniformly -----------------------


def raise_locked_if_needed(retry_sec: int | None) -> None:
    if retry_sec is not None:
        raise AccountLockedError(
            "Account temporarily locked due to too many failed attempts.",
            retry_after=retry_sec,
        )
