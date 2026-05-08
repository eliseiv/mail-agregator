"""HTTP routes for the groups module (ADR-0019).

JSON API: ``/api/admin/groups`` (super-admin only).
HTML pages: ``/admin/groups``, ``/admin/groups/new``, ``/admin/groups/{id}/edit``.

Form-encoded fallback (ADR-0015):

- ``POST /api/admin/groups`` — create.
- ``POST /api/admin/groups/{id}`` + ``_method=PATCH`` — rename.
- ``POST /api/admin/groups/{id}/delete`` + ``_method=DELETE`` — delete.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path, Query, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import ValidationError as PydanticValidationError

from backend.app.deps import (
    AdminOrLeaderScope,
    CurrentScope,
    DbSession,
    SuperAdminScope,
    is_form_request,
)
from backend.app.exceptions import (
    DomainError,
    ValidationError,
)
from backend.app.flash import flash
from backend.app.groups.schemas import (
    GroupCreateRequest,
    GroupDetailDTO,
    GroupsListResponse,
    GroupUpdateRequest,
)
from backend.app.groups.service import GroupsService
from backend.app.rate_limit import LIMIT_ADMIN_WRITE, client_ip, consume
from backend.app.repositories.users import UsersRepo
from backend.app.templates import render
from shared.models import ROLE_GROUP_MEMBER

api = APIRouter(prefix="/api/admin/groups", tags=["groups"])
html = APIRouter(prefix="/admin/groups", tags=["groups-html"])


# ---------------------------------------------------------------------------
# Form parsing
# ---------------------------------------------------------------------------


def _form_str(form: object, name: str) -> str:
    if not hasattr(form, "get"):
        return ""
    v = form.get(name)
    return v if isinstance(v, str) else ""


def _form_int_list(form: object, name: str) -> list[int]:
    """Read a multi-valued form field (``name[]`` or repeated ``name``) as ints.

    Empty or missing field → empty list. Non-integer entries raise a
    ``ValidationError`` with field=name.
    """
    if not hasattr(form, "getlist"):
        return []
    raw_items: list[str] = []
    # Starlette's FormData supports both ``name`` and ``name[]`` keys.
    raw_items.extend([v for v in form.getlist(name) if isinstance(v, str)])
    raw_items.extend([v for v in form.getlist(f"{name}[]") if isinstance(v, str)])
    out: list[int] = []
    for raw in raw_items:
        s = raw.strip()
        if not s:
            continue
        try:
            out.append(int(s))
        except ValueError as exc:
            raise ValidationError(
                f"{name} must contain integers only",
                field=name,
            ) from exc
    return out


async def _parse_create_form(request: Request) -> GroupCreateRequest:
    form = await request.form()
    raw_leader = _form_str(form, "leader_user_id").strip()
    try:
        leader_id = int(raw_leader)
    except ValueError as exc:
        raise ValidationError(
            "leader_user_id must be an integer",
            field="leader_user_id",
        ) from exc
    member_ids = _form_int_list(form, "member_ids")
    try:
        return GroupCreateRequest.model_validate(
            {
                "name": _form_str(form, "name"),
                "leader_user_id": leader_id,
                "member_ids": member_ids,
            }
        )
    except PydanticValidationError as exc:
        raise ValidationError("Invalid form payload") from exc


async def _parse_update_form(request: Request) -> GroupUpdateRequest:
    form = await request.form()
    name_raw = _form_str(form, "name").strip()
    payload: dict[str, object] = {}
    if name_raw:
        payload["name"] = name_raw
    try:
        return GroupUpdateRequest.model_validate(payload)
    except PydanticValidationError as exc:
        raise ValidationError("Invalid form payload") from exc


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------


@api.get("", response_model=GroupsListResponse)
async def list_groups(
    db: DbSession,
    scope: SuperAdminScope,
    q: Annotated[str | None, Query(max_length=64)] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> GroupsListResponse:
    return await GroupsService(db).list_for_scope(scope, q=q, page=page, limit=limit)


@api.get("/{group_id}", response_model=GroupDetailDTO)
async def get_group(
    db: DbSession,
    scope: SuperAdminScope,
    group_id: int = Path(..., ge=1),
) -> GroupDetailDTO:
    return await GroupsService(db).get_detail(scope, group_id)


@api.post(
    "",
    response_model=None,
    status_code=status.HTTP_201_CREATED,
)
async def create_group(
    request: Request,
    db: DbSession,
    scope: SuperAdminScope,
) -> Response:
    actor_id = scope.user_id
    await consume(LIMIT_ADMIN_WRITE, str(actor_id))
    ip = client_ip(request)
    ua = request.headers.get("user-agent", "")
    is_form = is_form_request(request)

    if is_form:
        payload = await _parse_create_form(request)
    else:
        body = await request.json()
        try:
            payload = GroupCreateRequest.model_validate(body)
        except PydanticValidationError as exc:
            raise ValidationError("Invalid JSON payload") from exc

    try:
        async with db.begin():
            dto = await GroupsService(db).create(
                actor=scope,
                name=payload.name,
                leader_user_id=payload.leader_user_id,
                member_ids=payload.member_ids,
                ip=ip,
                user_agent=ua,
            )
    except DomainError as exc:
        if is_form:
            await flash(request, "error", exc.message)
            return await _rerender_groups_new(
                request,
                db,
                error_message=exc.message,
                form_values={
                    "name": payload.name,
                    "leader_user_id": payload.leader_user_id,
                    "member_ids": payload.member_ids,
                },
                status_code=exc.status_code,
            )
        raise

    if is_form:
        await flash(request, "success", "Группа создана")
        return RedirectResponse(url="/admin/groups", status_code=status.HTTP_303_SEE_OTHER)
    return JSONResponse(
        content=dto.model_dump(mode="json"),
        status_code=status.HTTP_201_CREATED,
    )


async def _patch_group_impl(
    request: Request,
    db: DbSession,
    scope: AdminOrLeaderScope,
    group_id: int,
) -> Response:
    actor_id = scope.user_id
    await consume(LIMIT_ADMIN_WRITE, str(actor_id))
    ip = client_ip(request)
    ua = request.headers.get("user-agent", "")
    is_form = is_form_request(request)

    if is_form:
        payload = await _parse_update_form(request)
    else:
        body = await request.json()
        try:
            payload = GroupUpdateRequest.model_validate(body)
        except PydanticValidationError as exc:
            raise ValidationError("Invalid JSON payload") from exc

    if payload.name is None:
        if is_form:
            await flash(request, "error", "Имя группы обязательно")
            return RedirectResponse(
                url=f"/admin/groups/{group_id}/edit",
                status_code=status.HTTP_303_SEE_OTHER,
            )
        raise ValidationError("Nothing to update", field="name")

    try:
        async with db.begin():
            dto = await GroupsService(db).rename(
                actor=scope,
                group_id=group_id,
                name=payload.name,
                ip=ip,
                user_agent=ua,
            )
    except DomainError as exc:
        if is_form:
            await flash(request, "error", exc.message)
            return RedirectResponse(
                url=f"/admin/groups/{group_id}/edit",
                status_code=status.HTTP_303_SEE_OTHER,
            )
        raise

    if is_form:
        await flash(request, "success", "Группа переименована")
        return RedirectResponse(url="/admin/groups", status_code=status.HTTP_303_SEE_OTHER)
    return JSONResponse(content=dto.model_dump(mode="json"))


@api.patch("/{group_id}", response_model=None)
async def patch_group(
    request: Request,
    db: DbSession,
    scope: AdminOrLeaderScope,
    group_id: int = Path(..., ge=1),
) -> Response:
    return await _patch_group_impl(request, db, scope, group_id)


@api.post(
    "/{group_id}",
    response_model=None,
    include_in_schema=False,
)
async def patch_group_sibling(
    request: Request,
    db: DbSession,
    scope: AdminOrLeaderScope,
    group_id: int = Path(..., ge=1),
) -> Response:
    """Form-fallback to PATCH (POST + ``_method=PATCH``)."""
    return await _patch_group_impl(request, db, scope, group_id)


async def _delete_group_impl(
    request: Request,
    db: DbSession,
    scope: SuperAdminScope,
    group_id: int,
) -> Response:
    actor_id = scope.user_id
    await consume(LIMIT_ADMIN_WRITE, str(actor_id))
    ip = client_ip(request)
    ua = request.headers.get("user-agent", "")
    is_form = is_form_request(request)
    try:
        async with db.begin():
            await GroupsService(db).delete(
                actor=scope,
                group_id=group_id,
                ip=ip,
                user_agent=ua,
            )
    except DomainError as exc:
        if is_form:
            await flash(request, "error", exc.message)
            return RedirectResponse(url="/admin/groups", status_code=status.HTTP_303_SEE_OTHER)
        raise
    if is_form:
        await flash(request, "success", "Группа удалена")
        return RedirectResponse(url="/admin/groups", status_code=status.HTTP_303_SEE_OTHER)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@api.delete("/{group_id}", response_model=None)
async def delete_group(
    request: Request,
    db: DbSession,
    scope: SuperAdminScope,
    group_id: int = Path(..., ge=1),
) -> Response:
    return await _delete_group_impl(request, db, scope, group_id)


@api.delete(
    "/{group_id}/delete",
    response_model=None,
    include_in_schema=False,
)
async def delete_group_sibling(
    request: Request,
    db: DbSession,
    scope: SuperAdminScope,
    group_id: int = Path(..., ge=1),
) -> Response:
    return await _delete_group_impl(request, db, scope, group_id)


# ---------------------------------------------------------------------------
# HTML pages
# ---------------------------------------------------------------------------


async def _rerender_groups_new(
    request: Request,
    db: DbSession,
    *,
    error_message: str | None = None,
    form_values: dict[str, object] | None = None,
    status_code: int = 400,
) -> Response:
    sess = request.state.session
    candidates, _total = await UsersRepo(db).list_paged(
        q=None,
        page=1,
        limit=200,
        role=ROLE_GROUP_MEMBER,
    )
    return await render(
        request,
        "admin/groups/form.html",
        {
            "group": None,
            "candidates": candidates,
            "csrf_token": sess.csrf_token,
            "session": sess,
            "form": form_values or {},
            "error_message": error_message,
        },
        status_code=status_code,
    )


@html.get("", response_class=HTMLResponse)
@html.get("/", response_class=HTMLResponse)
async def groups_list_page(
    request: Request,
    db: DbSession,
    scope: CurrentScope,
    q: Annotated[str | None, Query(max_length=64)] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> Response:
    listing = await GroupsService(db).list_for_scope(scope, q=q, page=page, limit=limit)
    sess = request.state.session
    return await render(
        request,
        "admin/groups/list.html",
        {
            "groups": listing.items,
            "total": listing.total,
            "page": listing.page,
            "limit": listing.limit,
            "q": q or "",
            "is_super_admin": scope.is_super_admin,
            "csrf_token": sess.csrf_token,
            "session": sess,
        },
    )


@html.get("/new", response_class=HTMLResponse)
async def groups_new_page(
    request: Request,
    db: DbSession,
    scope: SuperAdminScope,
) -> Response:
    _ = scope  # auth
    return await _rerender_groups_new(request, db, status_code=200)


@html.get("/{group_id}/edit", response_class=HTMLResponse)
async def groups_edit_page(
    request: Request,
    db: DbSession,
    scope: AdminOrLeaderScope,
    group_id: int = Path(..., ge=1),
) -> Response:
    # FE-FIX round-5 #1: leader can open the edit page only for their own
    # group; ``get_detail`` enforces ownership for non-super_admin callers.
    detail = await GroupsService(db).get_detail(scope, group_id)
    sess = request.state.session
    return await render(
        request,
        "admin/groups/form.html",
        {
            "group": detail,
            "candidates": [],
            "csrf_token": sess.csrf_token,
            "session": sess,
            "form": {"name": detail.name},
            "is_super_admin": scope.is_super_admin,
        },
    )


router = APIRouter()
router.include_router(api)
router.include_router(html)
