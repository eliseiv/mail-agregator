"""Domain exception hierarchy + FastAPI handlers.

Wire format: ``docs/04-api-contracts.md`` "Унифицированный формат ошибок"::

    {"error": {"code": "snake_case", "message": "...", "field": "...", "details": {...}}}

HTML routes (no ``/api/`` prefix) get a Jinja-rendered error page when
applicable; JSON routes always get JSON.
"""

from __future__ import annotations

from typing import Any, cast

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ExceptionHandler

from shared.logging import get_logger

log = get_logger(__name__)


# --- Domain exceptions ------------------------------------------------------


class DomainError(Exception):
    """Base for all domain (non-HTTP-framework) errors.

    Attributes:
        status_code: HTTP status to return.
        code: machine-readable snake_case identifier.
        message: human-readable summary.
        field: optional pydantic-style field path.
        details: optional structured details (dict).
        retry_after: optional seconds for ``Retry-After`` header.
    """

    status_code: int = 500
    code: str = "internal_error"

    def __init__(
        self,
        message: str | None = None,
        *,
        field: str | None = None,
        details: dict[str, Any] | None = None,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(message or self.code)
        self.message = message or self.code
        self.field = field
        self.details = details or {}
        self.retry_after = retry_after


class NotAuthenticatedError(DomainError):
    status_code = 401
    code = "not_authenticated"


class InvalidCredentialsError(DomainError):
    status_code = 401
    code = "invalid_credentials"


class ForbiddenError(DomainError):
    status_code = 403
    code = "forbidden"


class CSRFError(DomainError):
    status_code = 403
    code = "csrf_failed"


class NotFoundError(DomainError):
    status_code = 404
    code = "not_found"


class ConflictError(DomainError):
    status_code = 409
    code = "conflict"


class ValidationError(DomainError):
    status_code = 400
    code = "validation_error"


class IMAPLoginFailedError(DomainError):
    status_code = 422
    code = "imap_login_failed"


class SMTPLoginFailedError(DomainError):
    status_code = 422
    code = "smtp_login_failed"


class SMTPSendFailedError(DomainError):
    status_code = 502
    code = "smtp_failed"


class InvalidHostError(DomainError):
    """SSRF guard rejected a hostname that resolves to a private network."""

    status_code = 422
    code = "invalid_host"


class AccountLockedError(DomainError):
    status_code = 423
    code = "account_locked"


class RateLimitedError(DomainError):
    status_code = 429
    code = "rate_limited"


class UpstreamError(DomainError):
    status_code = 502
    code = "upstream_error"


class DependencyUnavailableError(DomainError):
    status_code = 503
    code = "dependency_unavailable"


class CannotResetAdminError(DomainError):
    status_code = 400
    code = "cannot_reset_admin"


class CannotDeleteAdminError(DomainError):
    status_code = 400
    code = "cannot_delete_admin"


class CannotDeleteBuiltinTagError(DomainError):
    """User tried to ``DELETE /api/tags/{id}`` for a builtin tag (ADR-0017)."""

    status_code = 400
    code = "cannot_delete_builtin_tag"


class TagApplyTooManyError(DomainError):
    """``apply_to_existing=true`` rejected: user has > 100k messages.

    See ADR-0017 §7. Surfaced as 422 with code ``tag_apply_too_many``.
    """

    status_code = 422
    code = "tag_apply_too_many"


class TelegramLinkLimitError(DomainError):
    """User reached ``TG_MAX_LINKS_PER_USER`` active Telegram links
    (ADR-0024 §3). Surfaced as 409 ``tg_link_limit``."""

    status_code = 409
    code = "tg_link_limit"


class TelegramLinkOwnedByOtherError(DomainError):
    """The ``telegram_user_id`` is already linked to a *different* internal
    user; re-binding from an authenticated session is refused (ADR-0024 §4 —
    only the password login-flow may re-bind). Surfaced as 409
    ``tg_link_owned_by_other``."""

    status_code = 409
    code = "tg_link_owned_by_other"


class OAuthReconsentRequiredError(DomainError):
    """Send/test attempted on an oauth_outlook account whose refresh token was
    invalidated (``oauth_needs_consent=true``) — ADR-0025 §9.1. The user must
    reconnect Outlook before the account can be used again."""

    status_code = 409
    code = "oauth_reconsent_required"


class CannotAddSuperAdminToGroupError(DomainError):
    """``POST /api/admin/users/{id}/groups`` targeted a super_admin (ADR-0030).

    super_admin sees everything; memberships would break the invariant
    ``super_admin → group_id IS NULL`` and "no rows in user_groups".
    """

    status_code = 400
    code = "cannot_add_super_admin_to_group"


class MembershipAlreadyExistsError(DomainError):
    """``POST /api/admin/users/{id}/groups`` for an existing membership
    (UNIQUE ``user_groups(user_id, group_id)``) — ADR-0030."""

    status_code = 409
    code = "membership_already_exists"


class CannotRemoveHomeMembershipError(DomainError):
    """``DELETE /api/admin/users/{id}/groups/{group_id}`` tried to remove the
    home membership (``group_id == users.group_id``) — ADR-0030. Change the
    home team via "move" (``PATCH /api/admin/users/{id}``)."""

    status_code = 400
    code = "cannot_remove_home_membership"


class MembershipNotFoundError(DomainError):
    """``DELETE /api/admin/users/{id}/groups/{group_id}`` for a membership the
    user does not have — ADR-0030."""

    status_code = 404
    code = "membership_not_found"


class CannotMoveGroupLeaderError(DomainError):
    """ "Move" (``PATCH /api/admin/users/{id}`` changing ``group_id``) attempted
    on a ``group_leader`` — ADR-0030. Would break the leader invariant; only
    "add to team" (additional membership) is allowed for leaders."""

    status_code = 409
    code = "cannot_move_group_leader"


class GroupNotFoundError(DomainError):
    """A referenced ``group_id`` does not exist (ADR-0030 membership add)."""

    status_code = 404
    code = "group_not_found"


class WebhookUrlPrivateIpError(DomainError):
    """Outbound webhook URL would target a private/loopback/link-local
    address — ADR-0023 §4.3 SSRF protection.
    """

    status_code = 400
    code = "webhook_url_private_ip"


# --- Helpers ----------------------------------------------------------------


def _is_json_route(request: Request) -> bool:
    """JSON for ``/api/...``, HTML otherwise."""
    path = request.url.path or ""
    return path.startswith("/api/")


def _payload(err: DomainError) -> dict[str, Any]:
    body: dict[str, Any] = {
        "error": {
            "code": err.code,
            "message": err.message,
        }
    }
    if err.field is not None:
        body["error"]["field"] = err.field
    if err.details:
        body["error"]["details"] = err.details
    return body


def _headers(err: DomainError) -> dict[str, str]:
    h: dict[str, str] = {}
    if err.retry_after is not None:
        h["Retry-After"] = str(err.retry_after)
    return h


# --- Handlers ---------------------------------------------------------------


def _domain_handler(request: Request, exc: DomainError) -> JSONResponse:
    log.info(
        "domain_error",
        code=exc.code,
        status=exc.status_code,
        path=request.url.path,
    )
    return JSONResponse(
        status_code=exc.status_code,
        content=_payload(exc),
        headers=_headers(exc),
    )


def _http_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    code = "http_error"
    if exc.status_code == 401:
        code = "not_authenticated"
    elif exc.status_code == 403:
        code = "forbidden"
    elif exc.status_code == 404:
        code = "not_found"
    elif exc.status_code == 405:
        code = "method_not_allowed"
    elif exc.status_code == 429:
        code = "rate_limited"
    log.info(
        "http_exception",
        status=exc.status_code,
        path=request.url.path,
    )
    body = {
        "error": {
            "code": code,
            "message": str(exc.detail) if exc.detail else code,
        }
    }
    return JSONResponse(status_code=exc.status_code, content=body)


def _validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    # ADR-0014: log only field paths and codes, never values.
    field_paths = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", ()))
        field_paths.append({"loc": loc, "type": err.get("type")})
    log.info(
        "validation_error",
        path=request.url.path,
        errors=field_paths,
    )
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={
            "error": {
                "code": "validation_error",
                "message": "Request validation failed",
                "details": {"errors": field_paths},
            }
        },
    )


def _unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
    # Never leak internals.
    log.error(
        "unhandled_exception",
        path=request.url.path,
        exc_type=type(exc).__name__,
        exc_info=True,
    )
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "error": {
                "code": "internal_error",
                "message": "An internal error occurred.",
            }
        },
    )


def install_exception_handlers(app: FastAPI) -> None:
    # Starlette's ``add_exception_handler`` is typed against
    # ``ExceptionHandler = Callable[[Request, Exception], Response]`` (covariant
    # in the exception type) but our handlers narrow the second arg to a
    # specific subclass for ergonomics. ``cast`` is the right tool: a future
    # signature drift in any handler will still be caught by mypy locally
    # because the handler bodies remain fully typed.
    app.add_exception_handler(DomainError, cast(ExceptionHandler, _domain_handler))
    app.add_exception_handler(StarletteHTTPException, cast(ExceptionHandler, _http_handler))
    app.add_exception_handler(RequestValidationError, cast(ExceptionHandler, _validation_handler))
    app.add_exception_handler(Exception, _unhandled_handler)
