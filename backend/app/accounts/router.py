"""HTTP routes for mail accounts.

JSON API: ``/api/mail-accounts/...``.
HTML pages: ``/accounts``, ``/accounts/new``, ``/accounts/{id}/edit``.

The state-changing endpoints accept both ``application/json`` and
``application/x-www-form-urlencoded`` (no-JS fallback, ADR-0015).

Visibility (ADR-0019 §7.1) is enforced via :class:`VisibilityScope`:

- super_admin sees + manages every mail account;
- group_leader / group_member see + manage every account in their group.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Path, Query, Request, Response, status
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
from backend.app.deps import CurrentScope, DbSession, is_form_request
from backend.app.exceptions import (
    DomainError,
    NotFoundError,
    ValidationError,
)
from backend.app.flash import flash
from backend.app.groups.service import GroupsService
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


_TRUTHY_FORM_VALUES: frozenset[str] = frozenset({"on", "true", "1", "yes", "y"})


def _form_str(form: object, field: str) -> str:
    if not hasattr(form, "get"):
        return ""
    v = form.get(field)
    return v if isinstance(v, str) else ""


def _form_str_or_none(form: object, field: str) -> str | None:
    s = _form_str(form, field)
    return s.strip() or None


def _form_int_or_none(form: object, field: str) -> int | None:
    s = _form_str(form, field).strip()
    if not s:
        return None
    try:
        return int(s)
    except ValueError as exc:
        raise ValidationError(f"{field} must be an integer", field=field) from exc


def _form_bool(form: object, field: str, *, default: bool = False) -> bool:
    s = _form_str(form, field).strip().lower()
    if not s:
        return default
    return s in _TRUTHY_FORM_VALUES


async def _parse_create_form(request: Request) -> MailAccountCreateRequest:
    form = await request.form()
    try:
        return MailAccountCreateRequest.model_validate(
            {
                "email": _form_str(form, "email"),
                "password": _form_str(form, "password"),
                "display_name": _form_str_or_none(form, "display_name"),
                "imap_host": _form_str(form, "imap_host"),
                "imap_port": _form_int_or_none(form, "imap_port") or 993,
                "imap_ssl": _form_bool(form, "imap_ssl", default=True),
                "smtp_host": _form_str(form, "smtp_host"),
                "smtp_port": _form_int_or_none(form, "smtp_port") or 465,
                "smtp_ssl": _form_bool(form, "smtp_ssl", default=True),
                "smtp_starttls": _form_bool(form, "smtp_starttls", default=False),
                "smtp_username": _form_str_or_none(form, "smtp_username"),
                "smtp_password": _form_str_or_none(form, "smtp_password"),
                "target_user_id": _form_int_or_none(form, "target_user_id"),
                # ADR-0031 §2: optional target team. An empty / missing
                # ``group_id`` form field leaves this None → service falls back
                # to the owner's home group (full backward compatibility).
                "group_id": _form_int_or_none(form, "group_id"),
            }
        )
    except PydanticValidationError as exc:
        raise ValidationError("Invalid form payload") from exc


async def _parse_update_form(request: Request) -> MailAccountUpdateRequest:
    form = await request.form()
    # ``display_name`` follows the same "field present and empty == clear"
    # convention as the admin user PATCH form: an explicitly-empty value
    # is treated as a request to clear the column to NULL. Detect "field
    # present but empty" via the multi-items list (``form.get`` collapses
    # empty strings to ``""``, indistinguishable from "not sent" otherwise).
    has_dn_field = any(k == "display_name" for k, _ in form.multi_items())
    raw_dn = _form_str(form, "display_name").strip()
    payload_kw: dict[str, object] = {
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
    if has_dn_field:
        if raw_dn:
            payload_kw["display_name"] = raw_dn
        else:
            payload_kw["clear_display_name"] = True
    # ADR-0031 §3: team transfer via form. Presence of the ``group_id`` form
    # field means "change the team" (mirrors the JSON key-presence rule). An
    # empty value clears it to NULL (service authorises that for super_admin
    # only). We set ``set_group_id`` explicitly so the schema's presence
    # inference (driven by the JSON key) does not also need the key here.
    has_group_field = any(k == "group_id" for k, _ in form.multi_items())
    if has_group_field:
        payload_kw["set_group_id"] = True
        payload_kw["group_id"] = _form_int_or_none(form, "group_id")
    try:
        return MailAccountUpdateRequest.model_validate(payload_kw)
    except PydanticValidationError as exc:
        raise ValidationError("Invalid form payload") from exc


async def _form_values_for_rerender(request: Request) -> dict[str, object]:
    """Snapshot of the submitted form values for re-rendering after error.

    Excludes secrets (password fields).
    """
    form = await request.form()
    return {
        "email": _form_str(form, "email"),
        "display_name": _form_str(form, "display_name"),
        "imap_host": _form_str(form, "imap_host"),
        "imap_port": _form_int_or_none(form, "imap_port") or 993,
        "imap_ssl": _form_bool(form, "imap_ssl", default=True),
        "smtp_host": _form_str(form, "smtp_host"),
        "smtp_port": _form_int_or_none(form, "smtp_port") or 465,
        "smtp_ssl": _form_bool(form, "smtp_ssl", default=True),
        "smtp_starttls": _form_bool(form, "smtp_starttls", default=False),
        "smtp_username": _form_str_or_none(form, "smtp_username"),
        "target_user_id": _form_int_or_none(form, "target_user_id"),
        "group_id": _form_int_or_none(form, "group_id"),
    }


# ---------------------------------------------------------------------------
# JSON endpoints
# ---------------------------------------------------------------------------


@api.get("", response_model=list[MailAccountDTO])
async def list_accounts(db: DbSession, scope: CurrentScope) -> list[MailAccountDTO]:
    return await MailAccountService(db).list_for_scope(scope)


@api.post(
    "/test",
    response_model=TestResult,
    status_code=status.HTTP_200_OK,
)
async def test_account(
    payload: MailAccountTestRequest, db: DbSession, scope: CurrentScope
) -> TestResult:
    await consume(LIMIT_ACCOUNT_TEST, str(scope.user_id))
    return await MailAccountService(db).test(payload, scope=scope)


@api.post(
    "",
    response_model=None,
    status_code=status.HTTP_201_CREATED,
)
async def create_account(
    request: Request,
    db: DbSession,
    scope: CurrentScope,
) -> Response:
    """Create a mail account. Accepts JSON or form-encoded (ADR-0015)."""
    await consume(LIMIT_ACCOUNT_WRITE, str(scope.user_id))
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
            dto = await MailAccountService(db).create(scope=scope, payload=payload)
    except DomainError as exc:
        if is_form:
            await flash(request, "error", exc.message)
            team_ctx = await _team_selector_context(db, scope)
            return await render(
                request,
                "accounts/form.html",
                {
                    "account": None,
                    "csrf_token": request.state.session.csrf_token,
                    "session": request.state.session,
                    "form": await _form_values_for_rerender(request),
                    "error_message": exc.message,
                    **team_ctx,
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
    scope: CurrentScope,
    account_id: int = Path(..., ge=1),
) -> MailAccountDTO:
    return await MailAccountService(db).get_for_scope(scope, account_id)


@api.patch("/{account_id}", response_model=None)
async def update_account(
    request: Request,
    db: DbSession,
    scope: CurrentScope,
    account_id: int = Path(..., ge=1),
) -> Response:
    """Update an account. Accepts JSON or (via method override) form-encoded."""
    await consume(LIMIT_ACCOUNT_WRITE, str(scope.user_id))
    is_form = is_form_request(request)

    if is_form:
        payload = await _parse_update_form(request)
    else:
        body = await request.json()
        try:
            payload = MailAccountUpdateRequest.model_validate(body)
        except PydanticValidationError as exc:
            raise ValidationError("Invalid JSON payload") from exc

    service = MailAccountService(db)
    try:
        async with db.begin():
            dto = await service.update(scope=scope, account_id=account_id, payload=payload)
    except DomainError as exc:
        if is_form:
            # Try to keep the edit form populated with persisted values.
            acc = await MailAccountsRepo(db).get_by_id(account_id)
            if acc is None:
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

    # ADR-0046 §2 (H5): the mailbox-status hook fires only here — OUTSIDE the
    # ``db.begin()`` block, i.e. strictly AFTER the COMMIT. The dispatcher loads
    # the live DB snapshot, so an enqueue from inside the transaction could be
    # served the pre-commit state and never be corrected. Best-effort: a Redis
    # outage must not fail an already-committed update.
    await service.flush_crm_status_events()

    if is_form:
        await flash(request, "success", "Изменения сохранены")
        return RedirectResponse(url="/accounts", status_code=status.HTTP_303_SEE_OTHER)
    return JSONResponse(content=dto.model_dump(mode="json"))


async def _delete_account_impl(
    request: Request,
    db: DbSession,
    scope: CurrentScope,
    account_id: int,
) -> Response:
    await consume(LIMIT_ACCOUNT_WRITE, str(scope.user_id))
    is_form = is_form_request(request)
    try:
        async with db.begin():
            await MailAccountService(db).delete(scope=scope, account_id=account_id)
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
    scope: CurrentScope,
    account_id: int = Path(..., ge=1),
) -> Response:
    return await _delete_account_impl(request, db, scope, account_id)


@api.delete(
    "/{account_id}/delete",
    response_model=None,
    include_in_schema=False,
)
async def delete_account_sibling(
    request: Request,
    db: DbSession,
    scope: CurrentScope,
    account_id: int = Path(..., ge=1),
) -> Response:
    """Sibling endpoint reachable from a plain HTML form via method override."""
    return await _delete_account_impl(request, db, scope, account_id)


@api.post(
    "/{account_id}/sync-now",
    response_model=None,
    status_code=status.HTTP_202_ACCEPTED,
)
async def sync_now(
    request: Request,
    db: DbSession,
    scope: CurrentScope,
    account_id: int = Path(..., ge=1),
) -> Response:
    await consume(LIMIT_ACCOUNT_SYNC, f"{scope.user_id}:{account_id}")
    is_form = is_form_request(request)
    try:
        await MailAccountService(db).force_sync(scope=scope, account_id=account_id)
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


async def _team_selector_context(db: DbSession, scope: CurrentScope) -> dict[str, object]:
    """Team-selector data for the account form / list (ADR-0031 §5).

    Single source of truth is :meth:`GroupsService.selectable_teams` — the same
    method that backs ``GET /api/my/groups`` (used by the AJAX path). Rendering
    these ``<option>``s server-side keeps the selector working without JS.

    Returns:
      - ``teams``        : ``[{id, name}]`` sorted by name (the caller's teams).
      - ``home_group_id``: default pre-selected team (``None`` for super_admin).
      - ``team_names``   : ``{group_id: name}`` lookup for labelling current team.
      - ``is_super_admin``/``is_group_member``: role flags driving UI rules.
    """
    my = await GroupsService(db).selectable_teams(scope)
    teams = [{"id": g.id, "name": g.name} for g in my.groups]
    return {
        "teams": teams,
        "home_group_id": my.home_group_id,
        "team_names": {g.id: g.name for g in my.groups},
        "is_super_admin": scope.is_super_admin,
        "is_group_member": scope.is_group_member,
    }


@html.get("/accounts", response_class=HTMLResponse)
async def accounts_page(
    request: Request,
    db: DbSession,
    scope: CurrentScope,
    status: Annotated[Literal["all", "active", "inactive"], Query()] = "all",
) -> Response:
    accounts = await MailAccountService(db).list_for_scope(scope, status=status)
    sess = request.state.session
    team_ctx = await _team_selector_context(db, scope)
    # The list/PATCH JSON DTO intentionally omits ``group_id`` (04-api-contracts
    # §"GET /api/mail-accounts"); the HTML page needs each account's current team
    # to label the transfer dialog. Read it straight from the rows (page-only
    # presentation data, not part of the JSON API shape).
    repo = MailAccountsRepo(db)
    visible_ids = await MailAccountService(db).visible_user_ids(scope)
    account_group: dict[int, int | None] = {}
    if accounts:
        if visible_ids is None:
            rows = await repo.list_by_ids([a.id for a in accounts])
        else:
            rows = await repo.list_by_ids(visible_ids)
        account_group = {r.id: r.group_id for r in rows}
    return await render(
        request,
        "accounts/list.html",
        {
            "accounts": accounts,
            "account_group": account_group,
            "scope": scope,
            "status_filter": status,
            "csrf_token": sess.csrf_token,
            "session": sess,
            **team_ctx,
        },
    )


@html.get("/accounts/new", response_class=HTMLResponse)
async def accounts_new_page(request: Request, db: DbSession, scope: CurrentScope) -> Response:
    sess = request.state.session
    team_ctx = await _team_selector_context(db, scope)
    return await render(
        request,
        "accounts/form.html",
        {
            "account": None,
            "csrf_token": sess.csrf_token,
            "session": sess,
            **team_ctx,
        },
    )


@html.get("/accounts/{account_id}/edit", response_class=HTMLResponse)
async def accounts_edit_page(
    request: Request,
    db: DbSession,
    scope: CurrentScope,
    account_id: int = Path(..., ge=1),
) -> Response:
    visible = await MailAccountService(db).visible_user_ids(scope)
    acc = await MailAccountsRepo(db).get_for_user_ids(visible, account_id)
    if acc is None:
        raise NotFoundError()
    sess = request.state.session
    # Edit form intentionally does NOT render the team selector (ADR-0031 §4.7);
    # team changes go through the dedicated "transfer" dialog on the list page.
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
