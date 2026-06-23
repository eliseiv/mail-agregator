"""FastAPI dependency callables.

- :func:`get_db` — provide an :class:`AsyncSession`.
- :func:`current_session` — return the cached session payload or 401.
- :func:`current_user` — load the :class:`User` row and check it still exists.
- :func:`require_super_admin` / :func:`require_admin_or_leader` — role gates.
- :func:`current_scope` — :class:`VisibilityScope` for read/list endpoints.

Visibility model (ADR-0019 §7):

- ``super_admin``  — sees all mail accounts / messages.
- ``group_leader`` — sees mail accounts and messages of every member of the
  group (including own).
- ``group_member`` — same scope as the leader.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Literal

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.exceptions import (
    ForbiddenError,
    NotAuthenticatedError,
    NotFoundError,
)
from backend.app.repositories.user_groups import UserGroupsRepo
from backend.app.repositories.users import UsersRepo
from backend.app.sessions import SessionData, SessionStore
from shared.db import get_session
from shared.models import (
    ROLE_GROUP_LEADER,
    ROLE_GROUP_MEMBER,
    ROLE_SUPER_ADMIN,
    User,
)

Role = Literal["super_admin", "group_leader", "group_member"]


async def get_db() -> AsyncSession:  # type: ignore[misc]
    """FastAPI dependency for an :class:`AsyncSession`."""
    async for session in get_session():
        yield session


DbSession = Annotated[AsyncSession, Depends(get_db)]


def current_session(request: Request) -> SessionData:
    """Return the resolved session payload or raise 401.

    The session is loaded by :class:`backend.app.middlewares.SessionMiddleware`.
    """
    sess: SessionData | None = getattr(request.state, "session", None)
    if sess is None:
        raise NotAuthenticatedError()
    return sess


CurrentSession = Annotated[SessionData, Depends(current_session)]


async def current_user(
    request: Request,
    db: DbSession,
    sess: CurrentSession,
) -> User:
    """Look up the user row referenced by the session.

    If the user was deleted between session creation and now, revoke the
    session and surface 401 (so the browser logs out).

    SQLAlchemy 2.x AsyncSession autobegins a transaction on the first
    statement (``session.get`` here). Route handlers that subsequently open
    their own write tx via ``async with db.begin():`` would then fail with
    ``InvalidRequestError: A transaction is already begun on this Session``.
    We close the autobegun read-tx here so the handler starts from a clean
    slate. Detached ORM instances are still safe to read because the
    sessionmaker uses ``expire_on_commit=False`` (see ``shared/db.py``).
    """
    repo = UsersRepo(db)
    user = await repo.get_by_id(sess.user_id)
    if user is None:
        # User vanished -> wipe the session and bounce the client.
        token = getattr(request.state, "session_token", None)
        if token:
            await SessionStore().revoke(token)
        # Discard the autobegun read-tx before raising so the surrounding
        # request lifecycle does not see a dangling transaction either.
        await db.rollback()
        raise NotAuthenticatedError("Session user no longer exists")
    # Close the autobegun read-tx so route handlers can open their own
    # ``async with db.begin():`` without hitting "transaction already begun".
    await db.commit()
    return user


CurrentUser = Annotated[User, Depends(current_user)]


# ---------------------------------------------------------------------------
# VisibilityScope (ADR-0019 §7)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class VisibilityScope:
    """Caller-relative authorisation scope.

    Built once per request from the active session and threaded through the
    service layer. Any read/list/write that depends on "what can this user
    see/touch" must consume a :class:`VisibilityScope`, not a raw
    ``user_id``. See :class:`backend.app.accounts.service.MailAccountService`
    and :class:`backend.app.messages.service.MessageService`.
    """

    user_id: int
    role: Role
    group_id: int | None  # "home"/primary team. None iff role == 'super_admin'
    # ADR-0030: every team the user is a member of (home + additional) from
    # ``user_groups``. Empty for super_admin (sees everything without
    # memberships). ``group_id`` always belongs to this set for non-admins.
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


async def build_scope(user: User, db: AsyncSession) -> VisibilityScope:
    """Construct a :class:`VisibilityScope` from a fresh DB row.

    ADR-0030: loads the user's full team set from ``user_groups`` so the
    scope carries ``group_ids`` (home + additional). ``super_admin`` has no
    memberships (and sees everything regardless), so its ``group_ids`` is
    empty. Trusts the DB invariants (CHECK + trigger) — no extra validation.
    """
    if user.role == ROLE_SUPER_ADMIN:
        group_ids: frozenset[int] = frozenset()
    else:
        members = await UserGroupsRepo(db).list_group_ids_for_user(user.id)
        group_ids = frozenset(members)
        # Defence-in-depth: the home membership is always mirrored in
        # ``user_groups`` (migration backfill + service sync), but if a row
        # ever drifted we still want the home team visible.
        if user.group_id is not None:
            group_ids |= {user.group_id}
    return VisibilityScope(
        user_id=user.id,
        role=user.role,  # type: ignore[arg-type]
        group_id=user.group_id,
        group_ids=group_ids,
    )


async def current_scope(user: CurrentUser, db: DbSession) -> VisibilityScope:
    """FastAPI dependency: build a :class:`VisibilityScope` for the request.

    ADR-0030: building a non-admin scope reads ``user_groups``, which
    autobegins a read transaction on the shared session. We close it here
    (mirroring :func:`current_user`) so route handlers that later open their
    own ``async with db.begin():`` don't hit "transaction already begun".
    """
    scope = await build_scope(user, db)
    await db.commit()
    return scope


CurrentScope = Annotated[VisibilityScope, Depends(current_scope)]


# ---------------------------------------------------------------------------
# Role gates
# ---------------------------------------------------------------------------


def require_super_admin(scope: CurrentScope) -> VisibilityScope:
    """Raise 403 unless the caller is the super-admin."""
    if scope.role != ROLE_SUPER_ADMIN:
        raise ForbiddenError("Super-admin only")
    return scope


SuperAdminScope = Annotated[VisibilityScope, Depends(require_super_admin)]


def require_admin_or_leader(scope: CurrentScope) -> VisibilityScope:
    """Raise 403 unless caller is super-admin or a group leader."""
    if scope.role not in (ROLE_SUPER_ADMIN, ROLE_GROUP_LEADER):
        raise ForbiddenError("Admin or group leader only")
    return scope


AdminOrLeaderScope = Annotated[VisibilityScope, Depends(require_admin_or_leader)]


# Backwards-compat: ``require_admin`` previously checked ``user.is_admin``;
# it now means "super_admin only" and returns the :class:`User` row to keep
# pre-ADR-0019 routers compiling. New code should use :data:`SuperAdminScope`.
def require_admin(user: CurrentUser) -> User:
    if user.role != ROLE_SUPER_ADMIN:
        raise ForbiddenError("Super-admin only")
    return user


AdminUser = Annotated[User, Depends(require_admin)]


def get_session_token(request: Request) -> str:
    token: str | None = getattr(request.state, "session_token", None)
    if not token:
        raise NotAuthenticatedError()
    return token


SessionToken = Annotated[str, Depends(get_session_token)]


# --- Generic ownership helper -----------------------------------------------


def assert_owns(*, owned_user_id: int, current_user_id: int) -> None:
    """Raise 404 (NOT 403, to avoid leaking existence) on ownership mismatch.

    All read-side ownership in this app uses NOT_FOUND on mismatch (per
    ``docs/04-api-contracts.md``: ``404 if not owned``).
    """
    if owned_user_id != current_user_id:
        raise NotFoundError()


# --- Content negotiation (ADR-0015 — no-JS fallback) ------------------------


def is_form_request(request: Request) -> bool:
    """Return ``True`` when the client is a plain HTML form (no JS).

    Per ADR-0015 / ``docs/04-api-contracts.md`` "Content negotiation":

    - **Form-client**: ``Content-Type`` begins with
      ``application/x-www-form-urlencoded`` AND ``Accept`` does *not*
      include ``application/json`` (typical browser ``Accept`` is
      ``text/html, ...``).
    - **JSON-client**: ``Content-Type: application/json`` *or*
      ``Accept`` contains ``application/json`` (fetch / curl / xhr).

    The check is deliberately strict: an explicit ``Accept: application/json``
    means "give me JSON even if I sent form-encoded" — used by HTML pages
    that fetch via JS but post a form for legacy reasons.
    """
    ct = request.headers.get("content-type", "")
    accept = request.headers.get("accept", "")
    if not ct.lower().startswith("application/x-www-form-urlencoded"):
        return False
    return "application/json" not in accept.lower()
