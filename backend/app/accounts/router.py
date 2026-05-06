"""HTTP routes for mail accounts.

JSON API: ``/api/mail-accounts/...``.
HTML pages: ``/accounts``, ``/accounts/new``, ``/accounts/{id}/edit``.

The state-changing endpoints accept both ``application/json`` (legacy AJAX)
and ``application/x-www-form-urlencoded`` (HTML forms / no-JS fallback,
ADR-0015). On form-encoded requests the response is a ``303 See Other``
redirect with a flash message; JSON callers keep their previous
``200``/``201``/``204`` behaviour.

For DELETE without JS the canonical ``DELETE /api/mail-accounts/{id}``
endpoint is unreachable from a plain HTML form (browsers can only emit
GET/POST). Frontend posts to the sibling
``POST /api/mail-accounts/{id}/delete`` with hidden ``_method=DELETE``;
:class:`backend.app.middlewares.MethodOverrideMiddleware` rewrites the
scope method to ``DELETE`` before reaching this handler.
"""

from __future__ import annotations

from fastapi import APIRouter, Path, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import ValidationError as PydanticValidationError

from backend.app.accounts.schemas import (
    MailAccountCreateRequest,
    MailAccountDTO,
    MailAccountTestRequest,
    MailAccountUpdateRequest,
    TestResult,
)
from backend.app.accounts.service import MailAccountService
from backend.app.deps import CurrentUser, DbSession, is_form_request
from backend.app.exceptions import (
    DomainError,
    NotFoundError,
    ValidationError,
)
from backend.app.flash import flash
from backend.app.rate_limit import (
    LIMIT_ACCOUNT_SYNC,
    LIMIT_ACCOUNT_TEST,
    LIMIT_ACCOUNT_WRITE,
    consume,
)
from backend.app.repositories.mail_accounts import MailAccountsRepo
from backend.app.templates import render

# JSON router
api = APIRouter(prefix="/api/mail-accounts", tags=["mail-accounts"])

# HTML router
html = APIRouter(tags=["mail-accounts-html"])


# ---------------------------------------------------------------------------
# Form parsing helpers
# ---------------------------------------------------------------------------


# Values a browser might send for an unchecked checkbox we want to read as
# True. Browsers omit unchecked checkboxes entirely, so the only case we
# really need to support is "checkbox-was-checked, value=on".
_TRUTHY_FORM_VALUES: frozenset[str] = frozenset({"on", "true", "1", "yes", "y"})


def _form_str(form: object, field: str) -> str:
    """Return the value of ``field`` as a string (empty string if missing)."""
    if not hasattr(form, "get"):
        return ""
    v = form.get(field)
    return v if isinstance(v, str) else ""


def _form_str_or_none(form: object, field: str) -> str | None:
    """Return the value of ``field`` as a non-empty string, or ``None``."""
    s = _form_str(form, field)
    return s.strip() or None


def _form_int_or_none(form: object, field: str) -> int | None:
    """Return the value of ``field`` parsed as int, or ``None``.

    Raises :class:`ValidationError` on non-empty non-integer input — callers
    are expected to surface that as a re-render or 400.
    """
    s = _form_str(form, field).strip()
    if not s:
        return None
    try:
        return int(s)
    except ValueError as exc:
        raise ValidationError(f"{field} must be an integer", field=field) from exc


def _form_bool(form: object, field: str, *, default: bool = False) -> bool:
    """Parse a checkbox / boolean field.

    Browsers send only checked checkboxes, so absence means ``False``. For
    PATCH we sometimes want "field not present -> keep current value" — that
    case is handled at the call site (use :func:`_form_optional_bool`).
    """
    s = _form_str(form, field).strip().lower()
    if not s:
        return default
    return s in _TRUTHY_FORM_VALUES


async def _parse_create_form(request: Request) -> MailAccountCreateRequest:
    """Parse create-account form body into the Pydantic schema."""
    form = await request.form()
    try:
        return MailAccountCreateRequest.model_validate(
            {
                "email": _form_str(form, "email"),
                "password": _form_str(form, "password"),
                "imap_host": _form_str(form, "imap_host"),
                "imap_port": _form_int_or_none(form, "imap_port") or 993,
                "imap_ssl": _form_bool(form, "imap_ssl", default=True),
                "smtp_host": _form_str(form, "smtp_host"),
                "smtp_port": _form_int_or_none(form, "smtp_port") or 465,
                "smtp_ssl": _form_bool(form, "smtp_ssl", default=True),
                "smtp_starttls": _form_bool(form, "smtp_starttls", default=False),
                "smtp_username": _form_str_or_none(form, "smtp_username"),
                "smtp_password": _form_str_or_none(form, "smtp_password"),
            }
        )
    except PydanticValidationError as exc:
        raise ValidationError("Invalid form payload") from exc


async def _parse_update_form(request: Request) -> MailAccountUpdateRequest:
    """Parse PATCH form body into the partial Pydantic schema.

    Empty optional strings (``smtp_username=``) mean ``None`` (clear).
    Empty ``password=`` means "do not change" (handled by the service).
    Checkboxes (``imap_ssl`` / ``smtp_ssl`` / ``smtp_starttls``) are
    treated as "the form represents the full intended state": present
    (``=on``) means True, absent means False. This deviates from the
    "partial update" semantics of JSON PATCH but matches what an HTML
    edit-form actually sends — a browser cannot distinguish "leave
    unchanged" from "uncheck", and the edit page always re-emits every
    checkbox.
    """
    form = await request.form()
    try:
        return MailAccountUpdateRequest.model_validate(
            {
                # Email is read-only on the edit form; omit if empty.
                "email": _form_str_or_none(form, "email"),
                "password": _form_str_or_none(form, "password"),
                "imap_host": _form_str_or_none(form, "imap_host"),
                "imap_port": _form_int_or_none(form, "imap_port"),
                "imap_ssl": _form_bool(form, "imap_ssl", default=False),
                "smtp_host": _form_str_or_none(form, "smtp_host"),
                "smtp_port": _form_int_or_none(form, "smtp_port"),
                "smtp_ssl": _form_bool(form, "smtp_ssl", default=False),
                "smtp_starttls": _form_bool(form, "smtp_starttls", default=False),
                "smtp_username": _form_str_or_none(form, "smtp_username"),
                "smtp_password": _form_str_or_none(form, "smtp_password"),
            }
        )
    except PydanticValidationError as exc:
        raise ValidationError("Invalid form payload") from exc


async def _form_values_for_rerender(request: Request) -> dict[str, object]:
    """Snapshot of the submitted form values for re-rendering after error.

    Excludes secrets (password fields) so they're never echoed back.
    """
    form = await request.form()
    return {
        "email": _form_str(form, "email"),
        "imap_host": _form_str(form, "imap_host"),
        "imap_port": _form_int_or_none(form, "imap_port") or 993,
        "imap_ssl": _form_bool(form, "imap_ssl", default=True),
        "smtp_host": _form_str(form, "smtp_host"),
        "smtp_port": _form_int_or_none(form, "smtp_port") or 465,
        "smtp_ssl": _form_bool(form, "smtp_ssl", default=True),
        "smtp_starttls": _form_bool(form, "smtp_starttls", default=False),
        "smtp_username": _form_str_or_none(form, "smtp_username"),
    }


# ---------------------------------------------------------------------------
# JSON: list / get / test / create / update / delete / force-sync
# ---------------------------------------------------------------------------


@api.get("", response_model=list[MailAccountDTO])
async def list_accounts(db: DbSession, user: CurrentUser) -> list[MailAccountDTO]:
    return await MailAccountService(db).list_for_user(user.id)


@api.post(
    "/test",
    response_model=TestResult,
    status_code=status.HTTP_200_OK,
)
async def test_account(
    payload: MailAccountTestRequest, db: DbSession, user: CurrentUser
) -> TestResult:
    await consume(LIMIT_ACCOUNT_TEST, str(user.id))
    return await MailAccountService(db).test(payload)


@api.post(
    "",
    response_model=None,  # mixed JSON / redirect for forms
    status_code=status.HTTP_201_CREATED,
)
async def create_account(
    request: Request,
    db: DbSession,
    user: CurrentUser,
) -> Response:
    """Create a mail account. Accepts JSON or form-encoded (ADR-0015)."""
    await consume(LIMIT_ACCOUNT_WRITE, str(user.id))
    is_form = is_form_request(request)

    if is_form:
        payload = await _parse_create_form(request)
    else:
        body = await request.json()
        try:
            payload = MailAccountCreateRequest.model_validate(body)
        except PydanticValidationError as exc:
            raise ValidationError("Invalid JSON payload") from exc

    try:
        async with db.begin():
            dto = await MailAccountService(db).create(user_id=user.id, payload=payload)
    except DomainError as exc:
        if is_form:
            await flash(request, "error", exc.message)
            return await render(
                request,
                "accounts/form.html",
                {
                    "account": None,
                    "csrf_token": request.state.session.csrf_token,
                    "session": request.state.session,
                    "form": await _form_values_for_rerender(request),
                    "error_message": exc.message,
                },
                status_code=exc.status_code,
            )
        raise

    if is_form:
        await flash(request, "success", "Email-аккаунт добавлен")
        return RedirectResponse(url="/accounts", status_code=status.HTTP_303_SEE_OTHER)
    return JSONResponse(
        content=dto.model_dump(mode="json"),
        status_code=status.HTTP_201_CREATED,
    )


@api.get("/{account_id}", response_model=MailAccountDTO)
async def get_account(
    db: DbSession,
    user: CurrentUser,
    account_id: int = Path(..., ge=1),
) -> MailAccountDTO:
    return await MailAccountService(db).get_for_user(user.id, account_id)


@api.patch("/{account_id}", response_model=None)
async def update_account(
    request: Request,
    db: DbSession,
    user: CurrentUser,
    account_id: int = Path(..., ge=1),
) -> Response:
    """Update an account. Accepts JSON or (via method override) form-encoded."""
    # Snapshot ORM-bound primitives before the write transaction. After a
    # rollback inside ``async with db.begin():`` the ``user`` instance's
    # attributes are expired; touching ``user.id`` in the ``except`` branch
    # would trigger a sync lazy-load and crash asyncpg with
    # ``MissingGreenlet`` (BUG-003).
    user_id = user.id

    await consume(LIMIT_ACCOUNT_WRITE, str(user_id))
    is_form = is_form_request(request)

    if is_form:
        payload = await _parse_update_form(request)
    else:
        body = await request.json()
        try:
            payload = MailAccountUpdateRequest.model_validate(body)
        except PydanticValidationError as exc:
            raise ValidationError("Invalid JSON payload") from exc

    try:
        async with db.begin():
            dto = await MailAccountService(db).update(
                user_id=user_id, account_id=account_id, payload=payload
            )
    except DomainError as exc:
        if is_form:
            # Try to load the current account to keep the edit form populated
            # with persisted values where the user didn't override them.
            acc = await MailAccountsRepo(db).get_for_user(user_id, account_id)
            if acc is None:
                # The account vanished between the form load and submit.
                raise NotFoundError() from exc
            await flash(request, "error", exc.message)
            return await render(
                request,
                "accounts/form.html",
                {
                    "account": acc,
                    "csrf_token": request.state.session.csrf_token,
                    "session": request.state.session,
                    "form": await _form_values_for_rerender(request),
                    "error_message": exc.message,
                },
                status_code=exc.status_code,
            )
        raise

    if is_form:
        await flash(request, "success", "Изменения сохранены")
        return RedirectResponse(url="/accounts", status_code=status.HTTP_303_SEE_OTHER)
    return JSONResponse(content=dto.model_dump(mode="json"))


async def _delete_account_impl(
    request: Request,
    db: DbSession,
    user: CurrentUser,
    account_id: int,
) -> Response:
    """Shared body for both ``DELETE /...`` and ``POST .../delete``."""
    await consume(LIMIT_ACCOUNT_WRITE, str(user.id))
    is_form = is_form_request(request)
    try:
        async with db.begin():
            await MailAccountService(db).delete(user_id=user.id, account_id=account_id)
    except DomainError as exc:
        if is_form:
            await flash(request, "error", exc.message)
            return RedirectResponse(url="/accounts", status_code=status.HTTP_303_SEE_OTHER)
        raise
    if is_form:
        await flash(request, "success", "Аккаунт удалён")
        return RedirectResponse(url="/accounts", status_code=status.HTTP_303_SEE_OTHER)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@api.delete(
    "/{account_id}",
    response_model=None,
)
async def delete_account(
    request: Request,
    db: DbSession,
    user: CurrentUser,
    account_id: int = Path(..., ge=1),
) -> Response:
    return await _delete_account_impl(request, db, user, account_id)


@api.delete(
    "/{account_id}/delete",
    response_model=None,
    include_in_schema=False,
)
async def delete_account_sibling(
    request: Request,
    db: DbSession,
    user: CurrentUser,
    account_id: int = Path(..., ge=1),
) -> Response:
    """Sibling endpoint reachable from a plain HTML form via method override.

    The HTML form posts to ``POST /api/mail-accounts/{id}/delete`` with
    ``_method=DELETE``. :class:`MethodOverrideMiddleware` rewrites the scope
    method to ``DELETE`` before this handler runs. A direct ``POST`` (no
    ``_method``) on this path is rejected by FastAPI with ``405 Method Not
    Allowed`` because we only register the DELETE verb here.
    """
    return await _delete_account_impl(request, db, user, account_id)


@api.post(
    "/{account_id}/sync-now",
    response_model=None,
    status_code=status.HTTP_202_ACCEPTED,
)
async def sync_now(
    request: Request,
    db: DbSession,
    user: CurrentUser,
    account_id: int = Path(..., ge=1),
) -> Response:
    await consume(LIMIT_ACCOUNT_SYNC, f"{user.id}:{account_id}")
    is_form = is_form_request(request)
    try:
        await MailAccountService(db).force_sync(user_id=user.id, account_id=account_id)
    except DomainError as exc:
        if is_form:
            await flash(request, "error", exc.message)
            return RedirectResponse(url="/accounts", status_code=status.HTTP_303_SEE_OTHER)
        raise
    if is_form:
        await flash(request, "success", "Синхронизация запущена")
        return RedirectResponse(url="/accounts", status_code=status.HTTP_303_SEE_OTHER)
    return JSONResponse(content={"queued": True}, status_code=status.HTTP_202_ACCEPTED)


# ---------------------------------------------------------------------------
# HTML pages
# ---------------------------------------------------------------------------


@html.get("/accounts", response_class=HTMLResponse)
async def accounts_page(request: Request, db: DbSession, user: CurrentUser) -> Response:
    accounts = await MailAccountService(db).list_for_user(user.id)
    sess = request.state.session
    return await render(
        request,
        "accounts/list.html",
        {
            "accounts": accounts,
            "csrf_token": sess.csrf_token,
            "session": sess,
        },
    )


@html.get("/accounts/new", response_class=HTMLResponse)
async def accounts_new_page(request: Request, user: CurrentUser) -> Response:
    sess = request.state.session
    # User dependency ensures auth — the parameter is intentionally unused here.
    _ = user
    return await render(
        request,
        "accounts/form.html",
        {
            "account": None,
            "csrf_token": sess.csrf_token,
            "session": sess,
        },
    )


@html.get("/accounts/{account_id}/edit", response_class=HTMLResponse)
async def accounts_edit_page(
    request: Request,
    db: DbSession,
    user: CurrentUser,
    account_id: int = Path(..., ge=1),
) -> Response:
    acc = await MailAccountsRepo(db).get_for_user(user.id, account_id)
    if acc is None:
        raise NotFoundError()
    sess = request.state.session
    return await render(
        request,
        "accounts/form.html",
        {
            "account": acc,
            "csrf_token": sess.csrf_token,
            "session": sess,
        },
    )


# Combined router export.
router = APIRouter()
router.include_router(api)
router.include_router(html)
