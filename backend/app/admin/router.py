"""HTTP routes for the admin module.

JSON: ``/api/admin/...``.
HTML: ``/admin``, ``/admin/audit``.

State-changing endpoints accept both ``application/json`` and
``application/x-www-form-urlencoded`` (no-JS fallback, ADR-0015).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Path, Query, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import ValidationError as PydanticValidationError

from backend.app.admin.schemas import (
    AuditListResponse,
    CreateUserRequest,
    CreateUserResponse,
    DeleteUserResponse,
    UsersListResponse,
)
from backend.app.admin.service import AdminService
from backend.app.deps import AdminUser, DbSession, is_form_request
from backend.app.exceptions import (
    DomainError,
    ValidationError,
)
from backend.app.flash import flash
from backend.app.rate_limit import LIMIT_ADMIN_WRITE, client_ip, consume
from backend.app.templates import render

api = APIRouter(prefix="/api/admin", tags=["admin"])
html = APIRouter(prefix="/admin", tags=["admin-html"])


# ---------------------------------------------------------------------------
# Form parsing helpers
# ---------------------------------------------------------------------------


async def _parse_create_user_form(request: Request) -> CreateUserRequest:
    form = await request.form()
    username_v = form.get("username", "")
    email_v = form.get("email", "")
    username = username_v if isinstance(username_v, str) else ""
    email_raw = email_v if isinstance(email_v, str) else ""
    email: str | None = email_raw.strip() or None
    try:
        return CreateUserRequest.model_validate({"username": username, "email": email})
    except PydanticValidationError as exc:
        raise ValidationError("Invalid form payload") from exc


async def _rerender_admin_users(
    request: Request,
    db: DbSession,
    *,
    admin_id: int | None,
    error_message: str | None = None,
    create_form: dict[str, str] | None = None,
    status_code: int = 400,
) -> Response:
    """Re-render the admin/users page with an error context.

    Accepts ``admin_id`` as a primitive (not the ORM ``User``) because this
    helper is invoked after a rolled-back ``async with db.begin():`` block —
    at that point the ORM instance's attributes are expired, and reading
    ``admin.id`` here would trigger a sync lazy-load that crashes the
    asyncpg driver with ``MissingGreenlet`` (BUG-003). Callers must extract
    primitives from the ORM ``User`` *before* opening the write transaction.
    """
    sess = request.state.session
    listing = await AdminService(db).list_users(q=None, page=1, limit=50)
    return await render(
        request,
        "admin/users.html",
        {
            "users": listing.items,
            "total": listing.total,
            "page": listing.page,
            "limit": listing.limit,
            "q": "",
            "current_admin_id": admin_id,
            "csrf_token": sess.csrf_token,
            "session": sess,
            "error_message": error_message,
            "create_form": create_form or {},
        },
        status_code=status_code,
    )


# ---------------------------------------------------------------------------
# JSON: users
# ---------------------------------------------------------------------------


@api.get("/users", response_model=UsersListResponse)
async def list_users(
    db: DbSession,
    admin: AdminUser,
    q: Annotated[str | None, Query(max_length=64)] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> UsersListResponse:
    _ = admin  # auth
    return await AdminService(db).list_users(q=q, page=page, limit=limit)


@api.post(
    "/users",
    response_model=None,
    status_code=status.HTTP_201_CREATED,
)
async def create_user(
    request: Request,
    db: DbSession,
    admin: AdminUser,
) -> Response:
    """Create a user. Accepts JSON or form-encoded (ADR-0015)."""
    # Snapshot ORM-bound primitives before the write transaction. After a
    # rollback inside ``async with db.begin():`` the ``admin`` instance's
    # attributes are expired; touching ``admin.id`` in the ``except`` branch
    # would trigger a sync lazy-load and crash asyncpg with
    # ``MissingGreenlet`` (BUG-003).
    admin_id = admin.id

    await consume(LIMIT_ADMIN_WRITE, str(admin_id))
    ip = client_ip(request)
    ua = request.headers.get("user-agent", "")
    is_form = is_form_request(request)

    if is_form:
        payload = await _parse_create_user_form(request)
    else:
        body = await request.json()
        try:
            payload = CreateUserRequest.model_validate(body)
        except PydanticValidationError as exc:
            raise ValidationError("Invalid JSON payload") from exc

    try:
        async with db.begin():
            result: CreateUserResponse = await AdminService(db).create_user(
                payload=payload,
                actor_id=admin_id,
                ip=ip,
                user_agent=ua,
            )
    except DomainError as exc:
        if is_form:
            await flash(request, "error", exc.message)
            return await _rerender_admin_users(
                request,
                db,
                admin_id=admin_id,
                error_message=exc.message,
                create_form={"username": payload.username, "email": payload.email or ""},
                status_code=exc.status_code,
            )
        raise

    if is_form:
        await flash(request, "success", "Пользователь создан")
        return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)
    return JSONResponse(
        content=result.model_dump(mode="json"),
        status_code=status.HTTP_201_CREATED,
    )


@api.post("/users/{user_id}/reset", response_model=None)
async def reset_password(
    request: Request,
    db: DbSession,
    admin: AdminUser,
    user_id: int = Path(..., ge=1),
) -> Response:
    await consume(LIMIT_ADMIN_WRITE, str(admin.id))
    ip = client_ip(request)
    ua = request.headers.get("user-agent", "")
    is_form = is_form_request(request)
    try:
        async with db.begin():
            await AdminService(db).reset_password(
                target_id=user_id,
                actor_id=admin.id,
                ip=ip,
                user_agent=ua,
            )
    except DomainError as exc:
        if is_form:
            await flash(request, "error", exc.message)
            return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)
        raise

    if is_form:
        await flash(request, "success", "Пароль сброшен")
        return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)
    return JSONResponse(content={"ok": True})


async def _delete_user_impl(
    request: Request,
    db: DbSession,
    admin: AdminUser,
    user_id: int,
) -> Response:
    """Shared body for both ``DELETE /...`` and ``POST .../delete`` (override)."""
    await consume(LIMIT_ADMIN_WRITE, str(admin.id))
    ip = client_ip(request)
    ua = request.headers.get("user-agent", "")
    is_form = is_form_request(request)
    try:
        async with db.begin():
            result: DeleteUserResponse = await AdminService(db).delete_user(
                target_id=user_id,
                actor_id=admin.id,
                ip=ip,
                user_agent=ua,
            )
    except DomainError as exc:
        if is_form:
            await flash(request, "error", exc.message)
            return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)
        raise

    if is_form:
        await flash(request, "success", "Пользователь удалён")
        return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)
    return JSONResponse(content=result.model_dump(mode="json"))


@api.delete("/users/{user_id}", response_model=None)
async def delete_user(
    request: Request,
    db: DbSession,
    admin: AdminUser,
    user_id: int = Path(..., ge=1),
) -> Response:
    return await _delete_user_impl(request, db, admin, user_id)


@api.delete(
    "/users/{user_id}/delete",
    response_model=None,
    include_in_schema=False,
)
async def delete_user_sibling(
    request: Request,
    db: DbSession,
    admin: AdminUser,
    user_id: int = Path(..., ge=1),
) -> Response:
    """Sibling endpoint reachable from a plain HTML form via method override.

    Browser form posts ``POST /api/admin/users/{id}/delete`` with hidden
    ``_method=DELETE``. :class:`MethodOverrideMiddleware` rewrites scope
    method to ``DELETE`` before this handler is matched. A direct ``POST``
    on this path returns ``405 Method Not Allowed``.
    """
    return await _delete_user_impl(request, db, admin, user_id)


# ---------------------------------------------------------------------------
# JSON: audit
# ---------------------------------------------------------------------------


@api.get("/audit", response_model=AuditListResponse)
async def list_audit(
    db: DbSession,
    admin: AdminUser,
    action: Annotated[str | None, Query(max_length=64)] = None,
    target_user_id: Annotated[int | None, Query(ge=1)] = None,
    from_date: Annotated[datetime | None, Query(alias="from")] = None,
    to_date: Annotated[datetime | None, Query(alias="to")] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> AuditListResponse:
    _ = admin
    return await AdminService(db).list_audit(
        action=action,
        target_user_id=target_user_id,
        from_date=from_date,
        to_date=to_date,
        page=page,
        limit=limit,
    )


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------


@html.get("", response_class=HTMLResponse)
@html.get("/", response_class=HTMLResponse)
async def admin_users_page(
    request: Request,
    db: DbSession,
    admin: AdminUser,
    q: Annotated[str | None, Query(max_length=64)] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> Response:
    sess = request.state.session
    listing = await AdminService(db).list_users(q=q, page=page, limit=limit)
    return await render(
        request,
        "admin/users.html",
        {
            "users": listing.items,
            "total": listing.total,
            "page": listing.page,
            "limit": listing.limit,
            "q": q or "",
            "current_admin_id": admin.id,
            "csrf_token": sess.csrf_token,
            "session": sess,
        },
    )


@html.get("/audit", response_class=HTMLResponse)
async def admin_audit_page(
    request: Request,
    db: DbSession,
    admin: AdminUser,
    action: Annotated[str | None, Query(max_length=64)] = None,
    target_user_id: Annotated[int | None, Query(ge=1)] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> Response:
    _ = admin
    sess = request.state.session
    listing = await AdminService(db).list_audit(
        action=action,
        target_user_id=target_user_id,
        from_date=None,
        to_date=None,
        page=page,
        limit=limit,
    )
    # Build query string for pagination links (preserves active filters).
    qs_parts = []
    if action:
        qs_parts.append(f"action={action}")
    if target_user_id is not None:
        qs_parts.append(f"target_user_id={target_user_id}")
    if limit != 50:
        qs_parts.append(f"limit={limit}")
    query_qs = "&".join(qs_parts) + ("&" if qs_parts else "")

    return await render(
        request,
        "admin/audit.html",
        {
            "items": listing.items,
            "total": listing.total,
            "page": listing.page,
            "limit": listing.limit,
            "filter": {
                "action": action or "",
                "target_user_id": target_user_id,
                "from": "",
                "to": "",
            },
            "available_actions": [
                "user_create",
                "user_delete",
                "password_reset",
                "admin_login",
                "admin_logout",
                "lockout_triggered",
                "account_auto_disabled",
            ],
            "query_qs": query_qs,
            "csrf_token": sess.csrf_token,
            "session": sess,
        },
    )


router = APIRouter()
router.include_router(api)
router.include_router(html)
