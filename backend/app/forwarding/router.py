"""HTTP routes for mail forwarding (ADR-0034 §2).

Endpoints (all under ``/api/forwarding/me`` — the ``me`` suffix is a
group-scoped identifier; super_admin overrides via ``?group_id=<int>``):

- ``GET    /api/forwarding/me``          → :class:`ForwardingDTO` (404 if missing)
- ``PUT    /api/forwarding/me``          → :class:`ForwardingDTO` (upsert: 200/201)
- ``DELETE /api/forwarding/me``          → 204
- ``DELETE /api/forwarding/me/delete``   → 204 (form-fallback sibling, ADR-0015)

The HTML page ``/my/integrations`` is served by the webhooks router; the
frontend agent adds a "Переадресация" section there. Both JSON and
form-encoded bodies are accepted per ADR-0015 — the HTML form submits
form-encoded with the ``_method=PUT`` / ``_method=DELETE`` override (the two
exact paths are whitelisted in :mod:`backend.app.middlewares`).

All state-changing endpoints are CSRF-protected by
:class:`backend.app.csrf.CSRFMiddleware` (not in the exempt list).

Authorisation (ADR-0034 §2):

- ``group_leader``: own group only, no ``?group_id`` query.
- ``super_admin``: any group via ``?group_id=<id>`` query (mandatory).
- ``group_member``: 403 ``forbidden`` on every endpoint.
"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request, Response, status
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import ValidationError as PydanticValidationError

from backend.app.deps import CurrentScope, CurrentUser, DbSession, is_form_request
from backend.app.exceptions import DomainError, ValidationError
from backend.app.flash import flash
from backend.app.forwarding.schemas import ForwardingUpsertRequest
from backend.app.forwarding.service import ForwardingService
from backend.app.rate_limit import (
    LIMIT_FORWARDING_DELETE,
    LIMIT_FORWARDING_UPDATE,
    consume,
)
from shared.logging import get_logger

log = get_logger(__name__)

api = APIRouter(prefix="/api/forwarding", tags=["forwarding"])

_FLASH_FORWARDING_SAVED = "Переадресация сохранена"
_FLASH_FORWARDING_DELETED = "Переадресация удалена"

_INTEGRATIONS_URL = "/my/integrations"

_TRUTHY_FORM_VALUES: frozenset[str] = frozenset({"on", "true", "1", "yes", "y"})


# --- Form parsing helpers ---------------------------------------------------


def _form_str(form: object, field: str) -> str:
    if not hasattr(form, "get"):
        return ""
    v = form.get(field)
    return v if isinstance(v, str) else ""


def _form_str_or_none(form: object, field: str) -> str | None:
    s = _form_str(form, field)
    return s.strip() or None


def _form_bool_or_none(form: object, field: str) -> bool | None:
    """Tri-state bool from a form: True/False/None.

    ``is_active`` may be absent (no change / default), explicitly checked
    (True), or explicitly unchecked (False — encoded via a sibling
    ``is_active_present=1`` hidden field, since unchecked HTML checkboxes
    don't submit at all). Mirrors the webhooks form helper.
    """
    if not hasattr(form, "multi_items"):
        return None
    has_field = any(k == field for k, _ in form.multi_items())
    if not has_field:
        has_present = any(k == f"{field}_present" for k, _ in form.multi_items())
        return False if has_present else None
    s = _form_str(form, field).strip().lower()
    if not s:
        return False
    return s in _TRUTHY_FORM_VALUES


def _client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    if request.client:
        return request.client.host
    return ""


def _user_agent(request: Request) -> str | None:
    return request.headers.get("user-agent", "")[:256] or None


def _rate_limit_key(scope: CurrentScope, group_id: int | None) -> str:
    """Per-group rate-limit key (natural key even before a row exists)."""
    if scope.is_super_admin and group_id:
        return str(group_id)
    return str(scope.group_id or 0)


async def _parse_upsert_body(request: Request) -> ForwardingUpsertRequest:
    """Parse the PUT body — JSON or form-encoded."""
    if is_form_request(request):
        form = await request.form()
        try:
            return ForwardingUpsertRequest.model_validate(
                {
                    "forward_to": _form_str_or_none(form, "forward_to"),
                    "is_active": _form_bool_or_none(form, "is_active"),
                }
            )
        except PydanticValidationError as exc:
            raise ValidationError("Invalid form payload") from exc
    try:
        body = await request.json()
    except ValueError as exc:
        raise ValidationError("Body is not valid JSON") from exc
    try:
        return ForwardingUpsertRequest.model_validate(body)
    except PydanticValidationError as exc:
        raise ValidationError("Invalid JSON payload") from exc


# --- Endpoints --------------------------------------------------------------


@api.get("/me", response_model=None)
async def get_my_forwarding(
    db: DbSession,
    scope: CurrentScope,
    user: CurrentUser,  # - dep ensures auth + session resolution
    group_id: int | None = Query(default=None, ge=1),
) -> Response:
    dto = await ForwardingService(db).get_for_scope(scope, override_group_id=group_id)
    return JSONResponse(content=dto.model_dump(mode="json"))


@api.put("/me", response_model=None)
async def upsert_my_forwarding(
    request: Request,
    db: DbSession,
    scope: CurrentScope,
    user: CurrentUser,
    group_id: int | None = Query(default=None, ge=1),
) -> Response:
    payload = await _parse_upsert_body(request)
    is_form = is_form_request(request)

    await consume(LIMIT_FORWARDING_UPDATE, _rate_limit_key(scope, group_id))

    try:
        async with db.begin():
            dto, created = await ForwardingService(db).upsert_for_scope(
                scope,
                forward_to=payload.forward_to,
                is_active=payload.is_active,
                override_group_id=group_id,
                ip=_client_ip(request),
                user_agent=_user_agent(request),
            )
    except DomainError as exc:
        if is_form:
            await flash(request, "error", exc.message)
            return RedirectResponse(url=_INTEGRATIONS_URL, status_code=status.HTTP_303_SEE_OTHER)
        raise

    if is_form:
        await flash(request, "success", _FLASH_FORWARDING_SAVED)
        return RedirectResponse(url=_INTEGRATIONS_URL, status_code=status.HTTP_303_SEE_OTHER)
    return JSONResponse(
        content=dto.model_dump(mode="json"),
        status_code=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
    )


async def _do_delete(
    request: Request,
    db: DbSession,
    scope: CurrentScope,
    group_id: int | None,
) -> Response:
    is_form = is_form_request(request)
    await consume(LIMIT_FORWARDING_DELETE, _rate_limit_key(scope, group_id))

    try:
        async with db.begin():
            await ForwardingService(db).delete_for_scope(
                scope,
                override_group_id=group_id,
                ip=_client_ip(request),
                user_agent=_user_agent(request),
            )
    except DomainError as exc:
        if is_form:
            await flash(request, "error", exc.message)
            return RedirectResponse(url=_INTEGRATIONS_URL, status_code=status.HTTP_303_SEE_OTHER)
        raise

    if is_form:
        await flash(request, "success", _FLASH_FORWARDING_DELETED)
        return RedirectResponse(url=_INTEGRATIONS_URL, status_code=status.HTTP_303_SEE_OTHER)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@api.delete("/me", response_model=None, status_code=status.HTTP_204_NO_CONTENT)
async def delete_my_forwarding(
    request: Request,
    db: DbSession,
    scope: CurrentScope,
    user: CurrentUser,
    group_id: int | None = Query(default=None, ge=1),
) -> Response:
    return await _do_delete(request, db, scope, group_id)


# Form-fallback sibling (ADR-0015): the HTML form posts to
# ``POST /api/forwarding/me/delete`` with ``_method=DELETE``; the method-
# override middleware rewrites it to DELETE on this exact path.
@api.delete("/me/delete", response_model=None, status_code=status.HTTP_204_NO_CONTENT)
async def delete_my_forwarding_form(
    request: Request,
    db: DbSession,
    scope: CurrentScope,
    user: CurrentUser,
    group_id: int | None = Query(default=None, ge=1),
) -> Response:
    return await _do_delete(request, db, scope, group_id)


router = APIRouter()
router.include_router(api)
