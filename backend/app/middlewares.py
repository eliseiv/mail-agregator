"""Cross-cutting middlewares: request id, security headers, session loader,
HTTP method override.

Order in :func:`backend.app.main.create_app`:

1. RequestIDMiddleware (outermost so every log line has request_id)
2. SecurityHeadersMiddleware
3. SessionMiddleware (populates ``request.state.session``)
4. MethodOverrideMiddleware (no-JS fallback — must run before CSRF so the
   CSRF check sees the effective method; ADR-0015)
5. CSRFMiddleware (depends on session loader)
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any
from urllib.parse import unquote_plus

import structlog
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from backend.app.sessions import SessionData, SessionStore
from shared.config import get_settings
from shared.logging import get_logger

_log = get_logger(__name__)

# ASGI 3 type aliases (mirroring the upstream Starlette type hints).
_Scope = MutableMapping[str, Any]
_Message = MutableMapping[str, Any]
_Receive = Callable[[], Awaitable[_Message]]
_Send = Callable[[_Message], Awaitable[None]]
_ASGIApp = Callable[[_Scope, _Receive, _Send], Awaitable[None]]


class RequestIDMiddleware(BaseHTTPMiddleware):
    """Generate ``X-Request-ID`` (or honour an inbound one), bind to logs.

    ``cycle_id``-style structlog context binding for HTTP requests.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        rid = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        # Make the id available to handlers and to logs in this request.
        request.state.request_id = rid
        token = structlog.contextvars.bind_contextvars(request_id=rid)
        try:
            response = await call_next(request)
        finally:
            structlog.contextvars.unbind_contextvars("request_id")
            del token
        response.headers["X-Request-ID"] = rid
        return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Set baseline security headers.

    HTML-page set is the strict one from ``docs/04-api-contracts.md``.
    JSON responses get the minimal subset (``X-Content-Type-Options``).
    """

    _CSP = (
        "default-src 'self'; "
        "img-src 'self' data:; "
        "style-src 'self'; "
        "script-src 'self'; "
        "form-action 'self'; "
        "frame-ancestors 'none'; "
        "base-uri 'self'"
    )
    _PERMISSIONS = "geolocation=(), camera=(), microphone=()"

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)

        ct = response.headers.get("content-type", "")
        is_html = ct.startswith("text/html")

        response.headers.setdefault("X-Content-Type-Options", "nosniff")

        if is_html:
            response.headers.setdefault("Content-Security-Policy", self._CSP)
            response.headers.setdefault("X-Frame-Options", "DENY")
            response.headers.setdefault("Referrer-Policy", "same-origin")
            response.headers.setdefault("Permissions-Policy", self._PERMISSIONS)
            response.headers.setdefault("Cache-Control", "no-store")
            if get_settings().is_prod:
                response.headers.setdefault(
                    "Strict-Transport-Security",
                    "max-age=31536000; includeSubDomains",
                )
        return response


class SessionMiddleware(BaseHTTPMiddleware):
    """Resolve ``mas_session`` cookie to a :class:`SessionData` (or None).

    Stores the result on ``request.state.session``. Routes use the
    ``current_session`` / ``current_user`` dependencies in :mod:`backend.app.deps`
    to enforce auth — middleware itself never blocks.
    """

    def __init__(self, app: object) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self._store = SessionStore()

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        session: SessionData | None = None
        token = request.cookies.get("mas_session")
        if token:
            session = await self._store.get(token)
            if session is not None:
                # Sliding TTL (ADR-0004).
                await self._store.touch(token, session)
        request.state.session = session
        request.state.session_token = token if session else None
        return await call_next(request)


# ---------------------------------------------------------------------------
# Method override (no-JS fallback) — ADR-0015
# ---------------------------------------------------------------------------

# Whitelist of routes for which a ``_method`` form field is honoured. Strings
# are exact-match paths; compiled regex patterns are used for ``{id}``-style
# routes. Keep in sync with ``docs/adr/ADR-0015-no-js-fallback.md`` and the
# Form column in ``docs/04-api-contracts.md`` sec. 8.
_OVERRIDE_EXACT_PATHS: frozenset[str] = frozenset(
    {
        "/api/messages/send",
        "/api/mail-accounts",
        "/api/admin/users",
    }
)
_OVERRIDE_REGEX_PATHS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^/api/mail-accounts/\d+$"),  # PATCH (override)
    re.compile(r"^/api/mail-accounts/\d+/delete$"),  # DELETE sibling
    re.compile(r"^/api/mail-accounts/\d+/sync-now$"),
    re.compile(r"^/api/admin/users/\d+/reset$"),
    re.compile(r"^/api/admin/users/\d+/delete$"),  # DELETE sibling
)

_ALLOWED_OVERRIDE_METHODS: frozenset[str] = frozenset({"DELETE", "PATCH", "PUT"})


def _is_whitelisted_path(path: str) -> bool:
    if path in _OVERRIDE_EXACT_PATHS:
        return True
    return any(p.match(path) for p in _OVERRIDE_REGEX_PATHS)


def _extract_method_from_form(body: bytes, content_type: str) -> str | None:
    """Best-effort extract ``_method`` from a urlencoded body.

    Mirrors the parser in :mod:`backend.app.csrf` — keeps the middleware
    free of FastAPI/Starlette form-machinery so the body stays untouched
    for downstream handlers.
    """
    if "application/x-www-form-urlencoded" not in content_type.lower():
        return None
    try:
        text = body.decode("utf-8", errors="replace")
    except UnicodeDecodeError:
        return None
    for pair in text.split("&"):
        if "=" not in pair:
            continue
        k, v = pair.split("=", 1)
        if k == "_method":
            return unquote_plus(v).strip().upper()
    return None


class MethodOverrideMiddleware:
    """Translate ``POST + _method=<X>`` into an effective HTTP method ``<X>``.

    Implemented as a pure ASGI middleware (not :class:`BaseHTTPMiddleware`)
    so it can buffer the request body once at the outermost form-aware
    layer and replay it deterministically through the rest of the stack.
    The Starlette ``BaseHTTPMiddleware`` wraps the ASGI receive stream in
    a way that makes re-injecting bodies fragile when nested; manipulating
    ``scope`` and ``receive`` at the ASGI level is the standard approach
    for method override (see how Rails / Django adapters do it).

    Per ADR-0015. Triggers only when **all** are true:

    - Request method is ``POST``;
    - ``Content-Type`` starts with ``application/x-www-form-urlencoded``
      (multipart and JSON are NOT inspected — multipart is not used by any
      whitelist endpoint);
    - The form-body contains a ``_method`` field whose value is one of
      ``DELETE``, ``PATCH``, ``PUT`` (case-insensitive);
    - The request path matches the whitelist.

    A ``_method`` value pointing at a *non*-whitelist path returns
    ``400 method_override_not_allowed`` — this catches accidental
    propagation to sensitive endpoints.
    """

    def __init__(self, app: _ASGIApp) -> None:
        self._app = app

    async def __call__(  # noqa: PLR0911 — flat early-return guards are clearer than nesting
        self, scope: _Scope, receive: _Receive, send: _Send
    ) -> None:
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return
        if scope.get("method") != "POST":
            await self._app(scope, receive, send)
            return

        content_type = ""
        for raw_k, raw_v in scope.get("headers", []):
            if raw_k.lower() == b"content-type":
                content_type = raw_v.decode("latin-1")
                break
        if not content_type.lower().startswith("application/x-www-form-urlencoded"):
            await self._app(scope, receive, send)
            return

        # Buffer the entire body (it's tiny — form-encoded request).
        body_chunks: list[bytes] = []
        more = True
        while more:
            message = await receive()
            if message["type"] != "http.request":
                # Disconnect before body finished — propagate as-is.
                await self._app(scope, _replay(body_chunks, [message]), send)
                return
            body_chunks.append(message.get("body", b"") or b"")
            more = bool(message.get("more_body", False))
        body = b"".join(body_chunks)

        method_value = _extract_method_from_form(body, content_type)

        # Build a replay-receive that hands the buffered body to downstream
        # consumers. Always provided, even when no override happens, so the
        # downstream CSRF middleware and the route handler share the same
        # body cache semantics regardless of code path.
        async def replay_receive() -> _Message:
            return {
                "type": "http.request",
                "body": body,
                "more_body": False,
            }

        if method_value is None or method_value == "":
            await self._app(scope, replay_receive, send)
            return

        path = scope.get("path", "") or ""

        if not _is_whitelisted_path(path):
            _log.info(
                "method_override_not_allowed",
                path=path,
                attempted_method=method_value,
            )
            response = JSONResponse(
                status_code=400,
                content={
                    "error": {
                        "code": "method_override_not_allowed",
                        "message": ("Method override is not permitted on this route."),
                    }
                },
            )
            await response(scope, replay_receive, send)
            return

        if method_value not in _ALLOWED_OVERRIDE_METHODS:
            await self._app(scope, replay_receive, send)
            return

        original_method = scope["method"]
        scope["method"] = method_value
        _log.debug(
            "method_override_applied",
            original_method=original_method,
            effective_method=method_value,
            path=path,
        )
        await self._app(scope, replay_receive, send)


def _replay(buffered: list[bytes], remaining: list[_Message]) -> _Receive:
    """Build a receive() coroutine that replays already-buffered chunks
    followed by any further messages collected before bail-out.

    Used only on the disconnect / unexpected-message path where we still
    want downstream code to see the protocol events that were already
    drained from the upstream receive.
    """
    queue: list[_Message] = []
    for i, chunk in enumerate(buffered):
        queue.append(
            {
                "type": "http.request",
                "body": chunk,
                "more_body": (i < len(buffered) - 1) or bool(remaining),
            }
        )
    queue.extend(remaining)

    async def receive() -> _Message:
        if queue:
            return queue.pop(0)
        return {"type": "http.disconnect"}

    return receive
