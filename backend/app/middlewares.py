"""Cross-cutting middlewares: request id + security headers.

Order in :func:`backend.app.main.create_app`:

1. RequestIDMiddleware (outermost so every log line has request_id)
2. SecurityHeadersMiddleware

ADR-0044 §5: ``SessionMiddleware`` (cookie sessions), ``MethodOverrideMiddleware``
(no-JS fallback, ADR-0015) and ``CSRFMiddleware`` served the HTML UI and went
away with it — the machine surface (``/api/external/*``, ``/healthz``) has
neither cookies nor forms.
"""

from __future__ import annotations

import uuid

import structlog
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from shared.config import get_settings
from shared.logging import get_logger

_log = get_logger(__name__)


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

    JSON responses get the minimal subset (``X-Content-Type-Options``). The
    strict HTML set still applies to the only HTML left in the service — the
    self-contained Outlook-OAuth callback page (ADR-0045 §2). Its CSP is
    ``'self'``-only: the Telegram WebApp SDK allowance went away with the UI.
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
