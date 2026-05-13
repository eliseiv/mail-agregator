"""HTTP routes for the admin module.

JSON: ``/api/admin/...``.
HTML: ``/admin``, ``/admin/audit``.

State-changing endpoints accept both ``application/json`` and
``application/x-www-form-urlencoded`` (no-JS fallback, ADR-0015).

Post-ADR-0019: list_users / users-page render group + role information
and the actor is a :class:`VisibilityScope` (not a raw User), so a future
"leader manages own group" UI shares the same controllers.
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
    UpdateUserRequest,
    UserDTO,
    UsersListResponse,
)
from backend.app.admin.service import AdminService
from backend.app.deps import (
    AdminOrLeaderScope,
    DbSession,
    SuperAdminScope,
    VisibilityScope,
    is_form_request,
)
from backend.app.exceptions import (
    DomainError,
    ValidationError,
)
from backend.app.flash import flash
from backend.app.groups.schemas import EligibleUsersResponse
from backend.app.groups.service import GroupsService
from backend.app.rate_limit import LIMIT_ADMIN_WRITE, client_ip, consume
from backend.app.repositories.groups import GroupsRepo
from backend.app.templates import render

api = APIRouter(prefix="/api/admin", tags=["admin"])
html = APIRouter(prefix="/admin", tags=["admin-html"])
leader_html = APIRouter(prefix="/my", tags=["leader-html"])


# ---------------------------------------------------------------------------
# Form parsing helpers
# ---------------------------------------------------------------------------


def _form_str(form: object, name: str) -> str:
    if not hasattr(form, "get"):
        return ""
    v = form.get(name)
    return v if isinstance(v, str) else ""


def _form_int_or_none(form: object, name: str) -> int | None:
    raw = _form_str(form, name).strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise ValidationError(f"{name} must be an integer", field=name) from exc


async def _parse_create_user_form(request: Request) -> CreateUserRequest:
    form = await request.form()
    # ``email`` is intentionally NOT parsed: the field was removed from the
    # public API. Form-encoded clients that still submit ``email=...`` will
    # have it silently ignored (forward-compat for old HTML caches).
    payload: dict[str, object] = {
        "username": _form_str(form, "username"),
        "display_name": _form_str(form, "display_name").strip() or None,
        "role": _form_str(form, "role").strip() or "group_member",
        "group_id": _form_int_or_none(form, "group_id"),
    }
    try:
        return CreateUserRequest.model_validate(payload)
    except PydanticValidationError as exc:
        raise ValidationError("Invalid form payload") from exc


async def _parse_update_user_form(request: Request) -> UpdateUserRequest:
    form = await request.form()
    raw_dn = _form_str(form, "display_name")
    raw_role = _form_str(form, "role").strip()
    raw_gid = _form_str(form, "group_id").strip()

    # Differentiate "field absent" from "field present and empty".
    has_dn_field = "display_name" in {k for k, _ in form.multi_items()}
    has_gid_field = "group_id" in {k for k, _ in form.multi_items()}

    payload: dict[str, object] = {}
    if has_dn_field:
        s = raw_dn.strip()
        if not s:
            payload["clear_display_name"] = True
        else:
            payload["display_name"] = s
    if raw_role:
        payload["role"] = raw_role
    if has_gid_field:
        if not raw_gid:
            payload["clear_group_id"] = True
        else:
            try:
                payload["group_id"] = int(raw_gid)
            except ValueError as exc:
                raise ValidationError("group_id must be an integer", field="group_id") from exc
    try:
        return UpdateUserRequest.model_validate(payload)
    except PydanticValidationError as exc:
        raise ValidationError("Invalid form payload") from exc


async def _rerender_admin_users(
    request: Request,
    db: DbSession,
    *,
    actor: VisibilityScope,
    error_message: str | None = None,
    create_form: dict[str, object] | None = None,
    status_code: int = 400,
) -> Response:
    sess = request.state.session
    listing = await AdminService(db).list_users(actor, q=None, page=1, limit=50)
    # Bring up the group dropdown choices for the create-user form.
    groups, _ = await GroupsRepo(db).list_all(q=None, page=1, limit=200)

    grouped: list[dict[str, object]] = []
    current_key: int | None = -1
    for u in listing.items:
        gid = u.group.id if u.group else None
        if gid != current_key:
            grouped.append(
                {
                    "group_id": gid,
                    "group_name": (u.group.name if u.group else None),
                    "users": [],
                }
            )
            current_key = gid
        grouped[-1]["users"].append(u)  # type: ignore[union-attr]

    return await render(
        request,
        "admin/users.html",
        {
            "users": listing.items,
            "user_groups": grouped,
            "total": listing.total,
            "page": listing.page,
            "limit": listing.limit,
            "q": "",
            "current_admin_id": actor.user_id,
            "csrf_token": sess.csrf_token,
            "session": sess,
            "groups": groups,
            "is_super_admin": actor.is_super_admin,
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
    actor: SuperAdminScope,
    q: Annotated[str | None, Query(max_length=64)] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    group_id: Annotated[int | None, Query(ge=1)] = None,
    role: Annotated[str | None, Query(max_length=32)] = None,
) -> UsersListResponse:
    return await AdminService(db).list_users(
        actor,
        q=q,
        page=page,
        limit=limit,
        group_id=group_id,
        role=role,
    )


@api.get("/users/eligible", response_model=EligibleUsersResponse)
async def list_eligible_users(
    db: DbSession,
    actor: SuperAdminScope,
) -> EligibleUsersResponse:
    """Users that may be picked as leader / member in a new group.

    Excludes the super-admin (cannot be a member or leader by ADR-0019
    invariants). The frontend uses this to populate the multi-select
    dropdowns on the "create group" / "create user" forms.
    """
    return await GroupsService(db).list_eligible_users(actor)


@api.post(
    "/users",
    response_model=None,
    status_code=status.HTTP_201_CREATED,
)
async def create_user(
    request: Request,
    db: DbSession,
    actor: AdminOrLeaderScope,
) -> Response:
    actor_id = actor.user_id
    await consume(LIMIT_ADMIN_WRITE, str(actor_id))
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
                actor=actor,
                payload=payload,
                ip=ip,
                user_agent=ua,
            )
    except DomainError as exc:
        if is_form:
            await flash(request, "error", exc.message)
            return await _rerender_admin_users(
                request,
                db,
                actor=actor,
                error_message=exc.message,
                create_form={
                    "username": payload.username,
                    "display_name": payload.display_name or "",
                    "role": payload.role,
                    "group_id": payload.group_id,
                },
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


async def _patch_user_impl(
    request: Request,
    db: DbSession,
    actor: AdminOrLeaderScope,
    user_id: int,
) -> Response:
    actor_id = actor.user_id
    await consume(LIMIT_ADMIN_WRITE, str(actor_id))
    ip = client_ip(request)
    ua = request.headers.get("user-agent", "")
    is_form = is_form_request(request)

    if is_form:
        payload = await _parse_update_user_form(request)
    else:
        body = await request.json()
        try:
            payload = UpdateUserRequest.model_validate(body)
        except PydanticValidationError as exc:
            raise ValidationError("Invalid JSON payload") from exc

    try:
        async with db.begin():
            result: UserDTO = await AdminService(db).update_user(
                actor=actor,
                target_id=user_id,
                payload=payload,
                ip=ip,
                user_agent=ua,
            )
    except DomainError as exc:
        if is_form:
            await flash(request, "error", exc.message)
            return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)
        raise

    if is_form:
        await flash(request, "success", "Пользователь обновлён")
        return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)
    return JSONResponse(content=result.model_dump(mode="json"))


@api.patch("/users/{user_id}", response_model=None)
async def patch_user(
    request: Request,
    db: DbSession,
    actor: AdminOrLeaderScope,
    user_id: int = Path(..., ge=1),
) -> Response:
    return await _patch_user_impl(request, db, actor, user_id)


@api.post(
    "/users/{user_id}",
    response_model=None,
    include_in_schema=False,
)
async def patch_user_sibling(
    request: Request,
    db: DbSession,
    actor: AdminOrLeaderScope,
    user_id: int = Path(..., ge=1),
) -> Response:
    """Form-fallback to PATCH (POST + ``_method=PATCH``)."""
    return await _patch_user_impl(request, db, actor, user_id)


@api.post("/users/{user_id}/reset", response_model=None)
async def reset_password(
    request: Request,
    db: DbSession,
    actor: AdminOrLeaderScope,
    user_id: int = Path(..., ge=1),
) -> Response:
    await consume(LIMIT_ADMIN_WRITE, str(actor.user_id))
    ip = client_ip(request)
    ua = request.headers.get("user-agent", "")
    is_form = is_form_request(request)
    try:
        async with db.begin():
            await AdminService(db).reset_password(
                actor=actor,
                target_id=user_id,
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
    actor: AdminOrLeaderScope,
    user_id: int,
) -> Response:
    await consume(LIMIT_ADMIN_WRITE, str(actor.user_id))
    ip = client_ip(request)
    ua = request.headers.get("user-agent", "")
    is_form = is_form_request(request)
    try:
        async with db.begin():
            result: DeleteUserResponse = await AdminService(db).delete_user(
                actor=actor,
                target_id=user_id,
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
    actor: AdminOrLeaderScope,
    user_id: int = Path(..., ge=1),
) -> Response:
    return await _delete_user_impl(request, db, actor, user_id)


@api.delete(
    "/users/{user_id}/delete",
    response_model=None,
    include_in_schema=False,
)
async def delete_user_sibling(
    request: Request,
    db: DbSession,
    actor: AdminOrLeaderScope,
    user_id: int = Path(..., ge=1),
) -> Response:
    """Sibling endpoint reachable from a plain HTML form via method override."""
    return await _delete_user_impl(request, db, actor, user_id)


# ---------------------------------------------------------------------------
# JSON: audit
# ---------------------------------------------------------------------------


@api.get("/audit", response_model=AuditListResponse)
async def list_audit(
    db: DbSession,
    actor: SuperAdminScope,
    action: Annotated[str | None, Query(max_length=64)] = None,
    target_user_id: Annotated[int | None, Query(ge=1)] = None,
    from_date: Annotated[datetime | None, Query(alias="from")] = None,
    to_date: Annotated[datetime | None, Query(alias="to")] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> AuditListResponse:
    _ = actor
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
    actor: SuperAdminScope,
    q: Annotated[str | None, Query(max_length=64)] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> Response:
    sess = request.state.session
    listing = await AdminService(db).list_users(actor, q=q, page=page, limit=limit)
    groups, _ = await GroupsRepo(db).list_all(q=None, page=1, limit=200)
    # Bug-fix #2: orphan groups (no leader yet) are the only ones a new
    # group_leader can be assigned to. The JS filters the group dropdown
    # to these ids when role=group_leader is selected.
    orphan_group_ids = [g.id for g in groups if g.leader_user_id is None]

    # FE-FIX round-5 #2: pre-group users by group_id so the template can
    # render each group as a separate <tbody> with its own border + spacing.
    # Listing comes pre-sorted (NULLS FIRST → group_id → leader → id), so a
    # linear pass preserves the sort while bucketing.
    grouped: list[dict[str, object]] = []
    current_key: int | None = -1
    for u in listing.items:
        gid = u.group.id if u.group else None
        if gid != current_key:
            grouped.append(
                {
                    "group_id": gid,
                    "group_name": (u.group.name if u.group else None),
                    "users": [],
                }
            )
            current_key = gid
        grouped[-1]["users"].append(u)  # type: ignore[union-attr]

    return await render(
        request,
        "admin/users.html",
        {
            "users": listing.items,
            "user_groups": grouped,
            "total": listing.total,
            "page": listing.page,
            "limit": listing.limit,
            "q": q or "",
            "current_admin_id": actor.user_id,
            "groups": groups,
            "orphan_group_ids": orphan_group_ids,
            "is_super_admin": True,
            "csrf_token": sess.csrf_token,
            "session": sess,
        },
    )


@html.get("/audit", response_class=HTMLResponse)
async def admin_audit_page(
    request: Request,
    db: DbSession,
    actor: SuperAdminScope,
    action: Annotated[str | None, Query(max_length=64)] = None,
    target_user_id: Annotated[int | None, Query(ge=1)] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> Response:
    _ = actor
    sess = request.state.session
    listing = await AdminService(db).list_audit(
        action=action,
        target_user_id=target_user_id,
        from_date=None,
        to_date=None,
        page=page,
        limit=limit,
    )
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
                "create_user",
                "delete_user",
                "reset_password",
                "admin_login",
                "admin_logout",
                "lockout_triggered",
                "account_auto_disabled",
                "group_create",
                "group_delete",
                "group_rename",
                "user_role_change",
                "user_group_change",
            ],
            "query_qs": query_qs,
            "csrf_token": sess.csrf_token,
            "session": sess,
        },
    )


@leader_html.get("/members", response_class=HTMLResponse)
async def leader_members_page(
    request: Request,
    db: DbSession,
    actor: AdminOrLeaderScope,
) -> Response:
    """Group leader's "Участники" page (FE-FIX round-9 #1).

    Lists members of the leader's own group (incl. the leader). Provides a
    "+ Добавить участника" dialog (POST /api/admin/users with role auto-set
    to ``group_member`` and group_id forced to the leader's own — service
    layer handles this in :meth:`AdminService._resolve_create_role_and_group`).
    Reset / delete reuse the existing /api/admin/users endpoints, which
    accept group_leader callers and enforce same-group / non-leader
    invariants in the service layer.
    """
    sess = request.state.session
    listing = await AdminService(db).list_users(actor, q=None, page=1, limit=200)
    return await render(
        request,
        "admin/members.html",
        {
            "users": listing.items,
            "total": listing.total,
            "current_user_id": actor.user_id,
            "current_group_id": actor.group_id,
            "is_super_admin": actor.is_super_admin,
            "csrf_token": sess.csrf_token,
            "session": sess,
        },
    )


router = APIRouter()
router.include_router(api)
router.include_router(html)
router.include_router(leader_html)
