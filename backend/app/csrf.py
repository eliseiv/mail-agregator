"""CSRF middleware: double-submit cookie + server-side compare (ADR-0010).

Logic:

- Skip safe methods (GET, HEAD, OPTIONS) and explicit exempt paths.
- For ``POST/PUT/PATCH/DELETE`` extract the CSRF token from ``X-CSRF-Token``
  header **or** ``csrf_token`` form field.
- Look up the corresponding session — full session under ``mas_session`` for
  most paths, or setup-session under ``mas_setup`` for ``POST /set-password``.
- ``secrets.compare_digest`` against the token stored in the session payload.
- Mismatch / missing -> raise :class:`CSRFError` (403).

Exempt paths (no CSRF required):

- ``POST /login`` — there is no session yet; protected by rate-limit.
- ``GET /healthz``, ``GET /readyz``, static files.
"""

from __future__ import annotations

import secrets

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from backend.app.exceptions import CSRFError
from backend.app.sessions import SessionStore, SetupSessionStore
from shared.logging import get_logger

_log = get_logger(__name__)

SAFE_METHODS: frozenset[str] = frozenset({"GET", "HEAD", "OPTIONS"})

# Paths the CSRF middleware never inspects (exact match).
EXEMPT_PATHS: frozenset[str] = frozenset(
    {
        "/login",  # step-1 of two-step login (ADR-0016); no session yet
        "/login/password",  # step-2; protected by username|ip rate-limit
        "/healthz",
        "/readyz",
        # ADR-0022 §1.2: first call from Telegram WebApp; no session yet.
        # Defence is HMAC of ``init_data`` + ``auth_date`` TTL + rate-limit
        # per IP and per ``telegram_user_id``.
        "/api/telegram/auth",
    }
)

# Path prefixes that are CSRF-exempt. Used for routes that take a variable
# segment after a fixed prefix and cannot use the exact-match set.
# - ``/api/telegram/webhook/`` (ADR-0018): Telegram webhook; no user session
#   exists, secret-in-URL + ``X-Telegram-Bot-Api-Secret-Token`` header are
#   the proof-of-Telegram (see ``backend/app/telegram/router.py``).
EXEMPT_PATH_PREFIXES: tuple[str, ...] = ("/api/telegram/webhook/",)


def _is_exempt(path: str) -> bool:
    if path in EXEMPT_PATHS:
        return True
    return any(path.startswith(prefix) for prefix in EXEMPT_PATH_PREFIXES)


def _extract_token_from_form(form_body: bytes, content_type: str) -> str | None:
    """Best-effort extract ``csrf_token`` from a urlencoded body.

    For multipart/form-data we don't parse — clients of multipart are JS that
    can set the header instead.
    """
    if "application/x-www-form-urlencoded" not in content_type.lower():
        return None
    try:
        text = form_body.decode("utf-8", errors="replace")
    except UnicodeDecodeError:
        return None
    for pair in text.split("&"):
        if "=" not in pair:
            continue
        k, v = pair.split("=", 1)
        if k == "csrf_token":
            from urllib.parse import unquote_plus

            return unquote_plus(v)
    return None


class CSRFMiddleware(BaseHTTPMiddleware):
    """Verify CSRF token on state-changing requests."""

    def __init__(self, app: object) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self._sessions = SessionStore()
        self._setup = SetupSessionStore()

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        try:
            await self._verify(request)
        except CSRFError as exc:
            # ``BaseHTTPMiddleware.dispatch`` runs OUTSIDE the FastAPI
            # ``ExceptionMiddleware`` that ``install_exception_handlers``
            # registers handlers on, so a raised ``CSRFError`` would escape
            # the ASGI stack and the client would see a 500 / connection
            # error instead of the documented 403 (BUG-002). We translate
            # the domain error to a ``JSONResponse`` here using the same
            # envelope as :func:`backend.app.exceptions._domain_handler`.
            _log.info(
                "csrf_failed",
                code=exc.code,
                status=exc.status_code,
                path=request.url.path,
            )
            return JSONResponse(
                status_code=exc.status_code,
                content={
                    "error": {
                        "code": exc.code,
                        "message": exc.message,
                    }
                },
            )
        return await call_next(request)

    async def _verify(self, request: Request) -> None:
        """Run the CSRF check; raise :class:`CSRFError` on failure.

        Split out from :meth:`dispatch` so the latter owns the single
        try/except that converts the domain error into a wire response —
        keeping the parsing/comparison logic free of HTTP concerns.
        """
        if request.method in SAFE_METHODS or _is_exempt(request.url.path):
            return

        # ``/set-password`` uses the setup-session, not the main one.
        is_set_password = request.url.path == "/set-password"

        # Header token first.
        header_token = request.headers.get("X-CSRF-Token")

        body_token: str | None = None
        if not header_token:
            content_type = request.headers.get("content-type", "")
            if "application/x-www-form-urlencoded" in content_type.lower():
                # Body is cached by Starlette's ``_CachedRequest`` once
                # ``request.body()`` is awaited; downstream handlers (and
                # the form-aware ``MethodOverrideMiddleware`` running below
                # us) re-read it without a second wire round-trip. Manual
                # re-injection of ``request._receive`` would be a no-op
                # because ``call_next`` does not propagate that attribute.
                body_bytes = await request.body()
                body_token = _extract_token_from_form(body_bytes, content_type)

        submitted = header_token or body_token

        if not submitted:
            raise CSRFError("Missing CSRF token")

        expected: str | None = None
        if is_set_password:
            setup_token = request.cookies.get("mas_setup")
            if setup_token:
                ss = await self._setup.get(setup_token)
                if ss is not None:
                    expected = ss.csrf_token
        else:
            session_token = request.cookies.get("mas_session")
            if session_token:
                sd = await self._sessions.get(session_token)
                if sd is not None:
                    expected = sd.csrf_token

        if expected is None:
            raise CSRFError("No active session for CSRF check")
        if not secrets.compare_digest(expected, submitted):
            raise CSRFError("CSRF token mismatch")
