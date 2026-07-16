"""Domain exception hierarchy + FastAPI handlers.

Wire format: ``docs/04-api-contracts.md`` "Унифицированный формат ошибок"::

    {"error": {"code": "snake_case", "message": "...", "field": "...", "details": {...}}}

Every route answers JSON. The HTML branch (a Jinja-rendered error page for
non-``/api/`` routes) went away with the UI (ADR-0041 / ADR-0044 A3), and the
``_is_json_route`` helper that selected it with it (TD-060).
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


# ADR-0044 §5 (phase A3): ``CSRFError`` (``csrf_failed``) went away with the
# session/CSRF middleware and the HTML UI — the connector's only surface is the
# machine API (``/api/external/*``), which is key-authenticated and CSRF-exempt
# by construction. Nothing raised it any more.


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


class RateLimitedError(DomainError):
    status_code = 429
    code = "rate_limited"


class UpstreamError(DomainError):
    status_code = 502
    code = "upstream_error"


class DependencyUnavailableError(DomainError):
    status_code = 503
    code = "dependency_unavailable"


class OAuthReconsentRequiredError(DomainError):
    """Send/test attempted on an oauth_outlook account whose refresh token was
    invalidated (``oauth_needs_consent=true``) — ADR-0025 §9.1. The user must
    reconnect Outlook before the account can be used again."""

    status_code = 409
    code = "oauth_reconsent_required"


# ADR-0044 §5 (phases A1/A3) + TD-060: the admin/tags/groups/memberships/
# Telegram-link/webhook domain errors went away with the routes that raised
# them (session UI, tags, teams admin, outbound webhooks). Only the errors the
# machine API (``/api/external/*``) and the sync/send path can still raise
# remain above.


# --- Helpers ----------------------------------------------------------------


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
