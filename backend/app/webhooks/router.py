"""HTTP routes for outbound webhooks (ADR-0023 §2).

Endpoints (all under ``/api/webhooks/me`` — the ``me`` suffix is a
group-scoped identifier; super_admin overrides via ``?group_id=<int>``):

- ``GET    /api/webhooks/me``                 → :class:`WebhookDTO` (404 if missing)
- ``POST   /api/webhooks/me``                 → :class:`WebhookCreatedDTO`
- ``PATCH  /api/webhooks/me``                 → :class:`WebhookDTO`
- ``DELETE /api/webhooks/me``                 → 204
- ``POST   /api/webhooks/me/rotate-secret``   → :class:`WebhookCreatedDTO`
- ``POST   /api/webhooks/me/test``            → :class:`WebhookTestResult`
- ``GET    /my/integrations``                 → HTML page

All state-changing endpoints are CSRF-protected by :class:`CSRFMiddleware`
(they are not in the exempt list). Both JSON and form-encoded bodies are
accepted per ADR-0015 — the HTML page submits form-encoded with the
``_method=PATCH``/``=DELETE`` override.

Authorisation (ADR-0023 §2):

- ``group_leader``: own group only, no ``?group_id`` query.
- ``super_admin``: any group via ``?group_id=<id>`` query (mandatory).
- ``group_member``: 403 ``forbidden`` on every endpoint.
"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import ValidationError as PydanticValidationError

from backend.app.deps import CurrentScope, CurrentUser, DbSession, is_form_request
from backend.app.exceptions import (
    DomainError,
    ValidationError,
)
from backend.app.flash import flash
from backend.app.rate_limit import (
    LIMIT_WEBHOOK_CREATE,
    LIMIT_WEBHOOK_DELETE,
    LIMIT_WEBHOOK_ROTATE,
    LIMIT_WEBHOOK_TEST,
    LIMIT_WEBHOOK_UPDATE,
    Limit,
    consume,
)
from backend.app.templates import render
from backend.app.webhooks.schemas import (
    WebhookCreateRequest,
    WebhookDTO,
    WebhookTestResult,
    WebhookUpdateRequest,
)
from backend.app.webhooks.service import WebhooksService
from shared.config import get_settings
from shared.logging import get_logger

log = get_logger(__name__)

# JSON router (mounted under ``/api/webhooks``).
api = APIRouter(prefix="/api/webhooks", tags=["webhooks"])

# HTML router (mounted at ``/my/integrations``).
html = APIRouter(tags=["webhooks-html"])


# Flash messages — Russian copy, same noqa pattern as tags/router.
_FLASH_WEBHOOK_CREATED = "Интеграция создана. Секрет показан один раз — сохраните его."  # noqa: RUF001
_FLASH_WEBHOOK_UPDATED = "Интеграция обновлена"
_FLASH_WEBHOOK_DELETED = "Интеграция удалена"
_FLASH_WEBHOOK_SECRET_ROTATED = (
    "Новый секрет сгенерирован. Сохраните его — старый больше не работает."  # noqa: RUF001
)


# --- Form parsing helpers ---------------------------------------------------


_TRUTHY_FORM_VALUES: frozenset[str] = frozenset({"on", "true", "1", "yes", "y"})


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

    Used by the PATCH form where ``is_active`` may be absent (no
    change), explicitly checked (active=True), or explicitly unchecked
    (active=False — encoded via a sibling ``is_active_present=1`` hidden
    field, since unchecked HTML checkboxes don't submit at all).
    """
    if not hasattr(form, "multi_items"):
        return None
    has_field = any(k == field for k, _ in form.multi_items())
    if not has_field:
        # ``is_active_present`` lets the form distinguish "not submitted"
        # from "submitted unchecked". Without it a checkbox-off browser
        # submission is indistinguishable from "not on the form".
        has_present = any(k == f"{field}_present" for k, _ in form.multi_items())
        return False if has_present else None
    s = _form_str(form, field).strip().lower()
    if not s:
        return False
    return s in _TRUTHY_FORM_VALUES


async def _parse_create_body(request: Request) -> WebhookCreateRequest:
    """Parse the POST body — JSON or form-encoded."""
    if is_form_request(request):
        form = await request.form()
        try:
            return WebhookCreateRequest.model_validate({"url": _form_str(form, "url")})
        except PydanticValidationError as exc:
            raise ValidationError("Invalid form payload") from exc
    try:
        body = await request.json()
    except ValueError as exc:
        raise ValidationError("Body is not valid JSON") from exc
    try:
        return WebhookCreateRequest.model_validate(body)
    except PydanticValidationError as exc:
        raise ValidationError("Invalid JSON payload") from exc


async def _parse_update_body(request: Request) -> WebhookUpdateRequest:
    """Parse the PATCH body — JSON or form-encoded."""
    if is_form_request(request):
        form = await request.form()
        try:
            return WebhookUpdateRequest.model_validate(
                {
                    "url": _form_str_or_none(form, "url"),
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
        return WebhookUpdateRequest.model_validate(body)
    except PydanticValidationError as exc:
        raise ValidationError("Invalid JSON payload") from exc


def _client_ip(request: Request) -> str:
    """Best-effort client IP — same logic as
    :func:`backend.app.rate_limit.client_ip`."""
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    if request.client:
        return request.client.host
    return ""


# --- JSON endpoints ---------------------------------------------------------


@api.get("/me", response_model=WebhookDTO)
async def get_my_webhook(
    db: DbSession,
    scope: CurrentScope,
    user: CurrentUser,  # - dep ensures auth + session resolution
    group_id: int | None = Query(default=None, ge=1),
) -> WebhookDTO:
    return await WebhooksService(db).get_for_scope(scope, override_group_id=group_id)


@api.post("/me", response_model=None, status_code=status.HTTP_201_CREATED)
async def create_my_webhook(
    request: Request,
    db: DbSession,
    scope: CurrentScope,
    user: CurrentUser,
    group_id: int | None = Query(default=None, ge=1),
) -> Response:
    payload = await _parse_create_body(request)
    is_form = is_form_request(request)

    # Rate-limit per group_id (anti-spam after delete + recreate flow).
    # Group_id is the natural key here — even before the row exists.
    target_key = str(group_id) if scope.is_super_admin and group_id else str(scope.group_id or 0)
    await consume(LIMIT_WEBHOOK_CREATE, target_key)

    try:
        async with db.begin():
            dto = await WebhooksService(db).create_for_scope(
                scope,
                url=payload.url,
                override_group_id=group_id,
                ip=_client_ip(request),
                user_agent=request.headers.get("user-agent", "")[:256] or None,
            )
    except DomainError as exc:
        if is_form:
            await flash(request, "error", exc.message)
            return RedirectResponse(url="/my/integrations", status_code=status.HTTP_303_SEE_OTHER)
        raise

    if is_form:
        # Stash the secret in flash so the template can render the
        # one-time-show banner. ``secret_reveal`` is a custom category
        # the frontend agent matches on.
        await flash(request, "success", _FLASH_WEBHOOK_CREATED)
        await _set_secret_reveal(request, dto.secret)
        return RedirectResponse(url="/my/integrations", status_code=status.HTTP_303_SEE_OTHER)
    return JSONResponse(
        content=dto.model_dump(mode="json"),
        status_code=status.HTTP_201_CREATED,
    )


@api.patch("/me", response_model=None)
async def update_my_webhook(
    request: Request,
    db: DbSession,
    scope: CurrentScope,
    user: CurrentUser,
    group_id: int | None = Query(default=None, ge=1),
) -> Response:
    payload = await _parse_update_body(request)
    is_form = is_form_request(request)

    # Rate-limit per group (we don't have a webhook_id without a DB hit;
    # the keying matches LIMIT_WEBHOOK_CREATE semantics).
    target_key = str(group_id) if scope.is_super_admin and group_id else str(scope.group_id or 0)
    await consume(LIMIT_WEBHOOK_UPDATE, target_key)

    try:
        async with db.begin():
            dto = await WebhooksService(db).update_for_scope(
                scope,
                url=payload.url,
                is_active=payload.is_active,
                override_group_id=group_id,
                ip=_client_ip(request),
                user_agent=request.headers.get("user-agent", "")[:256] or None,
            )
    except DomainError as exc:
        if is_form:
            await flash(request, "error", exc.message)
            return RedirectResponse(url="/my/integrations", status_code=status.HTTP_303_SEE_OTHER)
        raise

    if is_form:
        await flash(request, "success", _FLASH_WEBHOOK_UPDATED)
        return RedirectResponse(url="/my/integrations", status_code=status.HTTP_303_SEE_OTHER)
    return JSONResponse(content=dto.model_dump(mode="json"))


@api.delete("/me", response_model=None, status_code=status.HTTP_204_NO_CONTENT)
async def delete_my_webhook(
    request: Request,
    db: DbSession,
    scope: CurrentScope,
    user: CurrentUser,
    group_id: int | None = Query(default=None, ge=1),
) -> Response:
    is_form = is_form_request(request)
    target_key = str(group_id) if scope.is_super_admin and group_id else str(scope.group_id or 0)
    await consume(LIMIT_WEBHOOK_DELETE, target_key)

    try:
        async with db.begin():
            await WebhooksService(db).delete_for_scope(
                scope,
                override_group_id=group_id,
                ip=_client_ip(request),
                user_agent=request.headers.get("user-agent", "")[:256] or None,
            )
    except DomainError as exc:
        if is_form:
            await flash(request, "error", exc.message)
            return RedirectResponse(url="/my/integrations", status_code=status.HTTP_303_SEE_OTHER)
        raise

    if is_form:
        await flash(request, "success", _FLASH_WEBHOOK_DELETED)
        return RedirectResponse(url="/my/integrations", status_code=status.HTTP_303_SEE_OTHER)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@api.post("/me/rotate-secret", response_model=None)
async def rotate_my_webhook_secret(
    request: Request,
    db: DbSession,
    scope: CurrentScope,
    user: CurrentUser,
    group_id: int | None = Query(default=None, ge=1),
) -> Response:
    is_form = is_form_request(request)
    target_key = str(group_id) if scope.is_super_admin and group_id else str(scope.group_id or 0)
    await consume(LIMIT_WEBHOOK_ROTATE, target_key)

    try:
        async with db.begin():
            dto = await WebhooksService(db).rotate_secret_for_scope(
                scope,
                override_group_id=group_id,
                ip=_client_ip(request),
                user_agent=request.headers.get("user-agent", "")[:256] or None,
            )
    except DomainError as exc:
        if is_form:
            await flash(request, "error", exc.message)
            return RedirectResponse(url="/my/integrations", status_code=status.HTTP_303_SEE_OTHER)
        raise

    if is_form:
        await flash(request, "success", _FLASH_WEBHOOK_SECRET_ROTATED)
        await _set_secret_reveal(request, dto.secret)
        return RedirectResponse(url="/my/integrations", status_code=status.HTTP_303_SEE_OTHER)
    return JSONResponse(content=dto.model_dump(mode="json"))


@api.post("/me/test", response_model=WebhookTestResult)
async def test_my_webhook(
    request: Request,
    db: DbSession,
    scope: CurrentScope,
    user: CurrentUser,
    group_id: int | None = Query(default=None, ge=1),
) -> WebhookTestResult:
    # Resolve target up-front so we can rate-limit per webhook_id.
    # We use the DB to look up the webhook id, then apply the limit.
    svc = WebhooksService(db)
    # ``get_for_scope`` raises 404 if missing — that's the correct UX
    # for "test" (no row to test).
    dto = await svc.get_for_scope(scope, override_group_id=group_id)

    # Per-webhook rate-limit, with operator-tunable capacity.
    settings = get_settings()
    runtime_limit = Limit(
        name=LIMIT_WEBHOOK_TEST.name,
        capacity=settings.WEBHOOK_TEST_LIMIT,
        window_seconds=LIMIT_WEBHOOK_TEST.window_seconds,
    )
    await consume(runtime_limit, f"wh:{dto.id}")

    return await svc.send_test(scope, override_group_id=group_id)


# --- Form-fallback sibling routes (ADR-0015) --------------------------------
# The form encoding on a plain HTML form can't easily issue DELETE/PATCH
# requests; the MethodOverrideMiddleware translates ``_method=DELETE`` /
# ``_method=PATCH`` POSTs into the real method, so the JSON endpoints above
# pick them up automatically. We add a sibling route for the "test" + "rotate"
# buttons that already use POST so the URLs stay shareable.


# --- HTML page --------------------------------------------------------------


@html.get("/my/integrations", response_class=HTMLResponse)
async def integrations_page(
    request: Request,
    db: DbSession,
    scope: CurrentScope,
    user: CurrentUser,
    group_id: int | None = Query(default=None, ge=1),
) -> Response:
    """Render ``/my/integrations``.

    For ``group_leader``: shows their group's webhook (or an empty form
    if not configured).

    For ``super_admin``: ``?group_id=<id>`` is required to populate the
    form; without it we render an empty selector hint (frontend agent
    decides how to surface the choice).

    For ``group_member``: 403 (same as the JSON endpoint).
    """
    if scope.is_group_member:
        return await render(
            request,
            "errors/403.html",
            {
                "session": getattr(request.state, "session", None),
            },
            status_code=status.HTTP_403_FORBIDDEN,
        )

    webhook: WebhookDTO | None = None
    error_message: str | None = None
    if scope.is_super_admin and group_id is None:
        # Render the page in "pick a group" mode — actual selector UX is
        # the frontend agent's call.
        webhook = None
    else:
        try:
            webhook = await WebhooksService(db).get_for_scope(scope, override_group_id=group_id)
        except DomainError as exc:
            if exc.code == "not_found":
                webhook = None
            else:
                error_message = exc.message

    secret_revealed = await _consume_secret_reveal(request)

    sess = request.state.session
    return await render(
        request,
        "my/integrations.html",
        {
            "webhook": webhook,
            "secret_revealed": secret_revealed,
            "error_message": error_message,
            "csrf_token": sess.csrf_token if sess is not None else "",
            "session": sess,
            "user": user,
            "scope": scope,
            "group_id_param": group_id,
        },
    )


# --- Secret-reveal helpers (one-shot flash for the HTML page) ---------------


_SECRET_REVEAL_REDIS_PREFIX = "webhook_secret_reveal:"
_SECRET_REVEAL_TTL_SECONDS = 60


async def _set_secret_reveal(request: Request, secret_plaintext: str) -> None:
    """Stash the just-generated secret in Redis with a 60s TTL so the
    follow-up GET of ``/my/integrations`` can show it once.

    Keyed by the session token so a second device cannot read the secret
    via the same flash. Cleared on read.
    """
    token: str | None = getattr(request.state, "session_token", None)
    if token is None:
        return
    from shared.redis_client import get_redis

    redis = get_redis()
    key = _SECRET_REVEAL_REDIS_PREFIX + token
    await redis.set(key, secret_plaintext, ex=_SECRET_REVEAL_TTL_SECONDS)


async def _consume_secret_reveal(request: Request) -> str | None:
    """Atomically read-and-delete the stashed secret for this session."""
    token: str | None = getattr(request.state, "session_token", None)
    if token is None:
        return None
    from shared.redis_client import get_redis

    redis = get_redis()
    key = _SECRET_REVEAL_REDIS_PREFIX + token
    async with redis.pipeline(transaction=True) as pipe:
        pipe.get(key)
        pipe.delete(key)
        results = await pipe.execute()
    raw = results[0]
    if raw is None:
        return None
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    return str(raw)


# Combined router export — same pattern as accounts / tags.
router = APIRouter()
router.include_router(api)
router.include_router(html)
