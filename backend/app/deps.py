"""FastAPI dependency callables.

- :func:`get_db` — provide an :class:`AsyncSession`.
- :func:`current_session` — return the cached session payload or 401.
- :func:`current_user` — load the :class:`User` row and check it still exists.
- :func:`require_admin` — like :func:`current_user` but enforces ``is_admin``.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.exceptions import (
    ForbiddenError,
    NotAuthenticatedError,
    NotFoundError,
)
from backend.app.repositories.users import UsersRepo
from backend.app.sessions import SessionData, SessionStore
from shared.db import get_session
from shared.models import User


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


def require_admin(user: CurrentUser) -> User:
    if not user.is_admin:
        raise ForbiddenError("Admin only")
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
