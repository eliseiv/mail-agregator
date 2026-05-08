"""Public auth routes: login, set-password, logout.

Per ``docs/04-api-contracts.md`` section 1 + ADR-0016 (two-step login).

Two-step login (ADR-0016):

- ``GET /login``                 — username-only form.
- ``POST /login``                — step-1: validates username, decides next
                                   page, sets ``mas_login`` cookie.
- ``GET /login/password``        — password form (username from cookie).
- ``POST /login/password``       — step-2: verifies password, creates session.
- ``GET /set-password``          — first-login password setup form.
- ``POST /set-password``         — store the new password, create session.
- ``POST /logout``               — destroy the session.
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import ValidationError as PydanticValidationError

from backend.app.auth.schemas import (
    LoginPasswordRequest,
    LoginUsernameRequest,
    SetPasswordRequest,
)
from backend.app.auth.service import AuthService, raise_locked_if_needed
from backend.app.cookies import (
    clear_login_cookie,
    clear_session_cookies,
    clear_setup_cookie,
    read_login_cookie,
    set_login_cookie,
    set_session_cookies,
    set_setup_cookie,
)
from backend.app.deps import DbSession, is_form_request
from backend.app.exceptions import (
    InvalidCredentialsError,
    NotAuthenticatedError,
    RateLimitedError,
    ValidationError,
)
from backend.app.rate_limit import (
    LIMIT_LOGIN,
    LIMIT_LOGIN_USERNAME,
    LIMIT_SET_PASSWORD,
    client_ip,
    consume,
)
from backend.app.repositories.users import UsersRepo
from backend.app.sessions import SetupSessionStore
from backend.app.templates import render
from shared.config import get_settings
from shared.logging import get_logger

log = get_logger(__name__)
router = APIRouter()


def _wants_json(request: Request) -> bool:
    accept = request.headers.get("accept", "")
    ct = request.headers.get("content-type", "")
    return "application/json" in accept or ct.startswith("application/json")


# ---------------------------------------------------------------------------
# Step 1: GET /login (username form)
# ---------------------------------------------------------------------------


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> Response:
    if getattr(request.state, "session", None) is not None:
        return RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)
    # Anonymous users have no CSRF cookie yet; we render an empty token. The
    # login form is exempt from CSRF (ADR-0010), so an empty value is fine.
    return await render(
        request,
        "login.html",
        {"csrf_token": "", "flash": None},
    )


# ---------------------------------------------------------------------------
# Step 1: POST /login (username only)
# ---------------------------------------------------------------------------


@router.post("/login")
async def login_username_submit(request: Request, db: DbSession) -> Response:
    """Step-1 of two-step login (ADR-0016).

    Accepts ``application/x-www-form-urlencoded`` or ``application/json``
    with a single ``username`` field. Resolves the next page (set-password
    vs password step) and sets the ``mas_login`` cookie that step-2 reads.

    To avoid user-enumeration, *both* the "user exists" and "user does
    not exist" branches forward to ``/login/password`` with the same
    cookie shape — only the existing-but-needs-setup branch diverges.

    Form-clients (no JS) get a HTML re-render with the field-level error
    on validation/rate-limit failures; JSON-clients get the canonical
    ``{"error": ...}`` body. Content negotiation via :func:`is_form_request`.
    """
    settings = get_settings()
    ip = client_ip(request)
    is_json = _wants_json(request)
    is_form = is_form_request(request)

    try:
        if is_json:
            body = await request.json()
            payload = LoginUsernameRequest.model_validate(body)
        else:
            form = await request.form()
            payload = LoginUsernameRequest.model_validate({"username": form.get("username", "")})
    except (PydanticValidationError, ValueError) as exc:
        if is_form:
            return await render(
                request,
                "login.html",
                {"csrf_token": "", "error_message": "Введите логин (3-64 символа)."},
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        raise ValidationError("Invalid login payload") from exc

    # Light per-IP rate-limit on the username step. Rationale: this endpoint
    # is itself an oracle (user_exists vs not via the redirect destination —
    # but ONLY for the set_password_required case, which we deliberately keep
    # for UX). 30/15min is high enough not to bother legitimate users while
    # preventing high-throughput enumeration.
    try:
        await consume(LIMIT_LOGIN_USERNAME, f"ip:{ip}")
    except RateLimitedError:
        if is_form:
            return await render(
                request,
                "login.html",
                {
                    "csrf_token": "",
                    "error_message": "Слишком много попыток. Повторите позже.",
                    "form_username": payload.username,
                },
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            )
        raise

    svc = AuthService(db)
    async with db.begin():
        lookup = await svc.lookup_for_login(username=payload.username)

    if lookup.kind == "set_password_required":
        assert lookup.setup_token is not None
        if is_json:
            response: Response = JSONResponse(
                content={
                    "kind": "set_password_required",
                    "redirect": "/set-password",
                }
            )
        else:
            response = RedirectResponse(
                url="/set-password",
                status_code=status.HTTP_303_SEE_OTHER,
            )
        set_setup_cookie(response, lookup.setup_token, settings)
        # Clear any stale step-1 cookie — we are not going to step-2.
        clear_login_cookie(response, settings)
        return response

    # Both ``ready_for_password`` and ``not_found`` flow through the same
    # branch — by design (see ADR-0016 / docs/06-security.md sec 1.1
    # "Information disclosure"). Stamping the cookie even when the user
    # does not exist costs nothing and keeps timings comparable.
    if is_json:
        response = JSONResponse(
            content={
                "kind": "needs_password",
                "redirect": "/login/password",
            }
        )
    else:
        response = RedirectResponse(
            url="/login/password",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    set_login_cookie(response, payload.username, settings)
    return response


# ---------------------------------------------------------------------------
# Step 2: GET /login/password (password form, username from cookie)
# ---------------------------------------------------------------------------


@router.get("/login/password", response_class=HTMLResponse)
async def login_password_page(request: Request) -> Response:
    if getattr(request.state, "session", None) is not None:
        return RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)
    username = read_login_cookie(request)
    if not username:
        # Step-1 was skipped (or the cookie expired) — bounce back.
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    return await render(
        request,
        "login_password.html",
        {"csrf_token": "", "username": username, "flash": None},
    )


# ---------------------------------------------------------------------------
# Step 2: POST /login/password (password verification, session creation)
# ---------------------------------------------------------------------------


async def _render_login_password_error(
    request: Request,
    *,
    username: str,
    error_message: str,
    status_code: int,
) -> Response:
    """Re-render ``login_password.html`` with an inline error.

    The form is exempt from CSRF (ADR-0010) so an empty ``csrf_token`` is
    acceptable. The password field is intentionally not echoed back —
    re-typing on every error is the safer UX (avoids accidental autofill
    on a shoulder-surfed input).
    """
    return await render(
        request,
        "login_password.html",
        {"csrf_token": "", "username": username, "error_message": error_message},
        status_code=status_code,
    )


@router.post("/login/password")
async def login_password_submit(  # noqa: PLR0911 - each return is a distinct outcome (form vs JSON x invalid/locked/limited/validation/setup/success)
    request: Request,
    db: DbSession,
) -> Response:
    """Step-2 of two-step login (ADR-0016).

    The username is recovered from the ``mas_login`` cookie set by step-1.
    From the user's perspective, behaviour for ``"user exists"`` and
    ``"user not found"`` is identical: a generic ``invalid_credentials``
    on bad password, the existing lockout/rate-limit logic on repeated
    failures.

    Form-clients get HTML re-renders of ``login_password.html`` with a
    Russian inline error on invalid-credentials / locked / rate-limited /
    validation-failed; JSON-clients keep the canonical error envelope.
    """
    settings = get_settings()
    ip = client_ip(request)
    ua = request.headers.get("user-agent", "")
    is_json = _wants_json(request)
    is_form = is_form_request(request)

    username = read_login_cookie(request)
    if not username:
        # Without the step-1 cookie we cannot identify the principal.
        # Treat as a stale form submission — bounce to /login.
        if is_json:
            raise NotAuthenticatedError("Login state expired; restart from /login")
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    try:
        if is_json:
            body = await request.json()
            payload = LoginPasswordRequest.model_validate(body)
        else:
            form = await request.form()
            payload = LoginPasswordRequest.model_validate(
                {
                    "password": form.get("password", ""),
                    "csrf_token": form.get("csrf_token") or None,
                }
            )
    except (PydanticValidationError, ValueError) as exc:
        if is_form:
            return await _render_login_password_error(
                request,
                username=username,
                error_message="Введите пароль.",
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        raise ValidationError("Invalid login payload") from exc

    # Rate-limit per username|IP (same key as the original single-step flow).
    try:
        await consume(LIMIT_LOGIN, f"{username}|{ip}")
    except RateLimitedError:
        if is_form:
            return await _render_login_password_error(
                request,
                username=username,
                error_message="Слишком много попыток. Повторите позже.",
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            )
        raise

    svc = AuthService(db)
    async with db.begin():
        result = await svc.login(
            username=username,
            password=payload.password,
            ip=ip,
            user_agent=ua,
        )

    if result.kind == "locked":
        if is_form:
            retry_sec = result.retry_after_sec or 0
            minutes = max(1, (retry_sec + 59) // 60)
            return await _render_login_password_error(
                request,
                username=username,
                error_message=(f"Слишком много неудачных попыток. Повторите через {minutes} мин."),
                status_code=status.HTTP_423_LOCKED,
            )
        raise_locked_if_needed(result.retry_after_sec)
        # raise_locked_if_needed always raises if retry > 0
        raise InvalidCredentialsError()  # pragma: no cover

    if result.kind == "invalid":
        if is_form:
            return await _render_login_password_error(
                request,
                username=username,
                error_message="Неверный пароль.",
                status_code=status.HTTP_401_UNAUTHORIZED,
            )
        raise InvalidCredentialsError("Invalid username or password.")

    if result.kind == "set_password_required":
        # The user was created with password_reset_required=true between
        # step-1 and step-2 (admin reset). Forward to set-password.
        assert result.setup_token is not None
        if is_json:
            response: Response = JSONResponse(
                content={
                    "kind": "set_password_required",
                    "redirect": "/set-password",
                }
            )
        else:
            response = RedirectResponse(
                url="/set-password",
                status_code=status.HTTP_303_SEE_OTHER,
            )
        set_setup_cookie(response, result.setup_token, settings)
        clear_login_cookie(response, settings)
        return response

    # session_created
    assert result.session_token and result.csrf
    if is_json:
        response = JSONResponse(
            content={
                "kind": "session_created",
                "redirect": settings.SAFE_REDIRECT_AFTER_LOGIN,
            }
        )
    else:
        response = RedirectResponse(
            url=settings.SAFE_REDIRECT_AFTER_LOGIN,
            status_code=status.HTTP_303_SEE_OTHER,
        )
    set_session_cookies(response, result.session_token, result.csrf, settings)
    clear_login_cookie(response, settings)
    return response


# ---------------------------------------------------------------------------
# GET /set-password
# ---------------------------------------------------------------------------


@router.get("/set-password", response_class=HTMLResponse)
async def set_password_page(request: Request, db: DbSession) -> Response:
    setup_token = request.cookies.get("mas_setup")
    if not setup_token:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
    setup = await SetupSessionStore().get(setup_token)
    if setup is None:
        # Expired
        response = RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
        clear_setup_cookie(response, get_settings())
        return response

    # Look up username to display in the form.
    user = await UsersRepo(db).get_by_id(setup.user_id)
    return await render(
        request,
        "set_password.html",
        {
            "csrf_token": setup.csrf_token,
            "username": user.username if user else "",
            "flash": None,
        },
    )


# ---------------------------------------------------------------------------
# POST /set-password
# ---------------------------------------------------------------------------


async def _render_set_password_error(
    request: Request,
    db: DbSession,
    *,
    setup_token: str,
    error_message: str,
    status_code: int,
) -> Response:
    """Re-render ``set_password.html`` with an inline error.

    Resolves the username from the still-valid setup-session so the page
    keeps showing the correct greeting after a failed submit. The CSRF
    token in the rendered form is the live one bound to the setup-session
    (re-rendered forms must keep CSRF validity for the next submission).
    """
    setup = await SetupSessionStore().get(setup_token)
    if setup is None:
        # Setup-session expired between submit and re-render — bounce.
        response = RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
        clear_setup_cookie(response, get_settings())
        return response
    user = await UsersRepo(db).get_by_id(setup.user_id)
    return await render(
        request,
        "set_password.html",
        {
            "csrf_token": setup.csrf_token,
            "username": user.username if user else "",
            "error_message": error_message,
        },
        status_code=status_code,
    )


@router.post("/set-password")
async def set_password_submit(
    request: Request,
    db: DbSession,
) -> Response:
    """Form-clients (no JS) get a HTML re-render of ``set_password.html``
    with a Russian inline error on weak/mismatched passwords or rate-limit;
    JSON-clients keep the canonical ``{"error": ...}`` envelope.

    We parse the form body manually (rather than using FastAPI's
    ``Form(...)`` parameters) so missing/empty fields hit our content-aware
    error path instead of FastAPI's global JSON validation handler.
    """
    settings = get_settings()
    is_form = is_form_request(request)
    setup_token = request.cookies.get("mas_setup")
    if not setup_token:
        if is_form:
            return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
        raise NotAuthenticatedError("Setup session missing")

    form = await request.form()
    password = str(form.get("password", "") or "")
    password_confirm = str(form.get("password_confirm", "") or "")
    # ``csrf_token`` is read by the CSRF middleware from the form body; we
    # do not need it here, but keep the read so a future migration to a
    # double-submit-only scheme has a single touchpoint.
    _ = form.get("csrf_token", "")

    # Rate-limit by setup-token per ADR-0009 (fallback to IP only when the
    # setup-cookie is missing — already handled above by the early return).
    rl_key = f"setup:{setup_token}"
    try:
        await consume(LIMIT_SET_PASSWORD, rl_key)
    except RateLimitedError:
        if is_form:
            return await _render_set_password_error(
                request,
                db,
                setup_token=setup_token,
                error_message="Слишком много попыток. Повторите позже.",
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            )
        raise

    if password != password_confirm:
        if is_form:
            return await _render_set_password_error(
                request,
                db,
                setup_token=setup_token,
                error_message="Пароли не совпадают.",
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        raise ValidationError("Passwords do not match", field="password_confirm")
    try:
        SetPasswordRequest.model_validate(
            {
                "password": password,
                "password_confirm": password_confirm,
                "csrf_token": "",
            }
        )
    except PydanticValidationError as exc:
        if is_form:
            return await _render_set_password_error(
                request,
                db,
                setup_token=setup_token,
                error_message=(
                    "Пароль не удовлетворяет требованиям: минимум 12 символов, "
                    "хотя бы одна буква и одна цифра."
                ),
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        raise ValidationError(
            "Password does not meet complexity requirements",
            field="password",
            details={"reason": str(exc.errors()[0]["msg"])},
        ) from exc

    ip = client_ip(request)
    ua = request.headers.get("user-agent", "")

    svc = AuthService(db)
    try:
        async with db.begin():
            result = await svc.complete_set_password(
                setup_token=setup_token,
                password=password,
                ip=ip,
                user_agent=ua,
            )
    except NotAuthenticatedError:
        # Setup-session expired (e.g. between GET and POST) — for form-clients
        # bounce to /login so the user can restart; JSON-clients get the
        # canonical 401.
        if is_form:
            response = RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
            clear_setup_cookie(response, settings)
            return response
        raise

    assert result.session_token and result.csrf
    response = RedirectResponse(
        url=settings.SAFE_REDIRECT_AFTER_LOGIN,
        status_code=status.HTTP_302_FOUND,
    )
    clear_setup_cookie(response, settings)
    clear_login_cookie(response, settings)
    set_session_cookies(response, result.session_token, result.csrf, settings)
    return response


# ---------------------------------------------------------------------------
# POST /logout
# ---------------------------------------------------------------------------


@router.post("/logout")
async def logout(request: Request, db: DbSession) -> Response:
    settings = get_settings()
    sess = getattr(request.state, "session", None)
    token: str | None = getattr(request.state, "session_token", None)
    if sess is None or token is None:
        # Nothing to do — just bounce to login.
        response = RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
        clear_session_cookies(response, settings)
        clear_login_cookie(response, settings)
        return response

    ip = client_ip(request)
    ua = request.headers.get("user-agent", "")
    svc = AuthService(db)
    async with db.begin():
        await svc.logout(
            session_token=token,
            actor_user_id=sess.user_id,
            is_admin=(sess.role == "super_admin"),
            ip=ip,
            user_agent=ua,
        )

    response = RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
    clear_session_cookies(response, settings)
    clear_login_cookie(response, settings)
    return response
