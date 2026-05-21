"""HTTP routes for the tags module (ADR-0017).

JSON: ``/api/tags/...``.
HTML pages: ``/tags`` (list), ``/tags/new`` (create form), ``/tags/{id}/edit``
(edit form).

State-changing endpoints accept both ``application/json`` and
``application/x-www-form-urlencoded`` (no-JS fallback, ADR-0015). The
form-encoded multi-row rules are parsed by walking parallel arrays
``rule_type[]`` / ``rule_pattern[]`` and dropping pairs where both sides
are empty.

DELETE without JS uses the sibling ``POST .../delete`` endpoints + hidden
``_method=DELETE`` (handled by :class:`MethodOverrideMiddleware`).
"""

from __future__ import annotations

from fastapi import APIRouter, Path, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import ValidationError as PydanticValidationError

from backend.app.deps import CurrentUser, DbSession, is_form_request
from backend.app.exceptions import (
    DomainError,
    ValidationError,
)
from backend.app.flash import flash
from backend.app.rate_limit import (
    LIMIT_TAGS_APPLY,
    LIMIT_TAGS_WRITE,
    consume,
)
from backend.app.tags.schemas import (
    PALETTE_COLORS,
    RuleSpec,
    TagCreateRequest,
    TagDTO,
    TagUpdateRequest,
)
from backend.app.tags.service import TagsService
from backend.app.templates import render

# JSON router (mounted under ``/api/tags``).
api = APIRouter(prefix="/api/tags", tags=["tags"])

# HTML router (no prefix; pages live at ``/tags``, ``/tags/new``, etc).
html = APIRouter(tags=["tags-html"])


# ---------------------------------------------------------------------------
# Flash messages — Russian copy from ``docs/04-api-contracts.md`` "Form-encoded
# fallback" section. Noqa-suppressed where the entire word consists of
# letters confusable with ASCII (ruff's RUF001 heuristic doesn't auto-detect
# the surrounding Cyrillic context in those cases).
# ---------------------------------------------------------------------------
_FLASH_TAG_CREATED = "Тег создан"  # noqa: RUF001
_FLASH_TAG_CREATED_APPLIED = "Тег создан, применён к {n} письмам"  # noqa: RUF001
_FLASH_TAG_UPDATED = "Тег обновлён"  # noqa: RUF001
_FLASH_TAG_DELETED = "Тег удалён"  # noqa: RUF001
_FLASH_RULE_ADDED = "Правило добавлено"
_FLASH_RULE_DELETED = "Правило удалено"
_FLASH_APPLIED_N = "Применено к {n} письмам"
_FLASH_INVALID_RULE = "Введите корректный тип и шаблон"


# ---------------------------------------------------------------------------
# Form parsing helpers
# ---------------------------------------------------------------------------


_TRUTHY_FORM_VALUES: frozenset[str] = frozenset({"on", "true", "1", "yes", "y"})


def _form_str(form: object, field: str) -> str:
    if not hasattr(form, "get"):
        return ""
    v = form.get(field)
    return v if isinstance(v, str) else ""


def _form_bool(form: object, field: str, *, default: bool = False) -> bool:
    s = _form_str(form, field).strip().lower()
    if not s:
        return default
    return s in _TRUTHY_FORM_VALUES


def _form_getlist(form: object, field: str) -> list[str]:
    """Return all values for ``field`` from a Starlette ``FormData`` object.

    ``FormData.getlist`` returns ``list[str | UploadFile]`` — we coerce to
    ``list[str]`` and drop anything else (no file uploads on tag forms).
    """
    if not hasattr(form, "getlist"):
        return []
    raw = form.getlist(field)
    return [v for v in raw if isinstance(v, str)]


def _parse_rules_from_form(form: object) -> list[RuleSpec]:
    """Pair ``rule_type[]`` and ``rule_pattern[]`` arrays into ``RuleSpec``.

    Empty pairs (both type AND pattern blank) are silently dropped — this
    is the documented no-JS UX where the template renders 5 fixed rule
    rows and the user fills in only the ones they need (see
    ``docs/08-frontend.md`` sec 4.11).

    A pair where one side is filled but the other is blank raises
    :class:`ValidationError` so the user sees the problem explicitly.
    """
    types = _form_getlist(form, "rule_type[]")
    patterns = _form_getlist(form, "rule_pattern[]")
    # Some browsers/template renders use ``name="rule_type"`` (no brackets);
    # accept that as a fallback for robustness.
    if not types and not patterns:
        types = _form_getlist(form, "rule_type")
        patterns = _form_getlist(form, "rule_pattern")
    if len(types) != len(patterns):
        raise ValidationError(
            "rule_type[] and rule_pattern[] must have the same length",
            field="rules",
        )
    out: list[RuleSpec] = []
    for t_raw, p_raw in zip(types, patterns, strict=True):
        t = t_raw.strip()
        p = p_raw.strip()
        if not t and not p:
            continue
        if not t or not p:
            raise ValidationError(
                "Each rule needs both a type and a pattern",
                field="rules",
            )
        try:
            out.append(RuleSpec(type=t, pattern=p))  # type: ignore[arg-type]
        except PydanticValidationError as exc:
            raise ValidationError("Invalid rule entry", field="rules") from exc
    return out


def _form_match_mode(form: object, *, default: str = "any") -> str:
    """Read ``match_mode`` from a form, defaulting to ``'any'``.

    Anything outside the ``{'any', 'all'}`` whitelist (including the empty
    string when the field is absent) collapses to the default — the radio in
    the UI only emits these two values, and Pydantic re-validates against the
    ``MatchMode`` literal downstream as defence-in-depth.
    """
    raw = _form_str(form, "match_mode").strip().lower()
    return raw if raw in {"any", "all"} else default


async def _parse_create_form(request: Request) -> TagCreateRequest:
    form = await request.form()
    name = _form_str(form, "name")
    color = _form_str(form, "color")
    match_mode = _form_match_mode(form)
    rules = _parse_rules_from_form(form)
    apply_to_existing = _form_bool(form, "apply_to_existing", default=False)
    try:
        return TagCreateRequest.model_validate(
            {
                "name": name,
                "color": color,
                "match_mode": match_mode,
                "rules": [r.model_dump() for r in rules],
                "apply_to_existing": apply_to_existing,
            }
        )
    except PydanticValidationError as exc:
        raise ValidationError("Invalid form payload") from exc


async def _parse_update_form(request: Request) -> TagUpdateRequest:
    form = await request.form()
    name_raw = _form_str(form, "name").strip()
    color_raw = _form_str(form, "color").strip()
    match_mode_raw = _form_str(form, "match_mode").strip().lower()
    payload: dict[str, str | None] = {}
    if name_raw:
        payload["name"] = name_raw
    if color_raw:
        payload["color"] = color_raw
    if match_mode_raw in {"any", "all"}:
        payload["match_mode"] = match_mode_raw
    try:
        return TagUpdateRequest.model_validate(payload)
    except PydanticValidationError as exc:
        raise ValidationError("Invalid form payload") from exc


async def _form_values_for_rerender(request: Request) -> dict[str, object]:
    """Snapshot of the create/edit form for re-rendering after error."""
    form = await request.form()
    types = _form_getlist(form, "rule_type[]") or _form_getlist(form, "rule_type")
    patterns = _form_getlist(form, "rule_pattern[]") or _form_getlist(form, "rule_pattern")
    paired: list[dict[str, str]] = []
    for t, p in zip(types, patterns, strict=False):
        paired.append({"type": t, "pattern": p})
    return {
        "name": _form_str(form, "name"),
        "color": _form_str(form, "color"),
        "match_mode": _form_match_mode(form),
        "rules": paired,
        "apply_to_existing": _form_bool(form, "apply_to_existing", default=False),
    }


# ---------------------------------------------------------------------------
# JSON endpoints
# ---------------------------------------------------------------------------


@api.get("", response_model=list[TagDTO])
async def list_tags(db: DbSession, user: CurrentUser) -> list[TagDTO]:
    return await TagsService(db).list_for_user(user.id)


@api.post(
    "",
    response_model=None,
    status_code=status.HTTP_201_CREATED,
)
async def create_tag(
    request: Request,
    db: DbSession,
    user: CurrentUser,
) -> Response:
    """Create a new tag. Accepts JSON or form-encoded (ADR-0015)."""
    user_id = user.id
    await consume(LIMIT_TAGS_WRITE, str(user_id))
    is_form = is_form_request(request)

    if is_form:
        payload = await _parse_create_form(request)
    else:
        body = await request.json()
        try:
            payload = TagCreateRequest.model_validate(body)
        except PydanticValidationError as exc:
            raise ValidationError("Invalid JSON payload") from exc

    try:
        async with db.begin():
            dto, applied = await TagsService(db).create(
                user_id=user_id,
                name=payload.name,
                color=payload.color,
                match_mode=payload.match_mode,
                rules=payload.rules,
                apply_to_existing=payload.apply_to_existing,
            )
    except DomainError as exc:
        if is_form:
            await flash(request, "error", exc.message)
            sess = request.state.session
            return await render(
                request,
                "tags/form.html",
                {
                    "tag": None,
                    "form": await _form_values_for_rerender(request),
                    "csrf_token": sess.csrf_token,
                    "session": sess,
                    "error_message": exc.message,
                    "is_edit": False,
                },
                status_code=exc.status_code,
            )
        raise

    if is_form:
        if applied > 0:
            await flash(
                request,
                "success",
                _FLASH_TAG_CREATED_APPLIED.format(n=applied),
            )
        else:
            await flash(request, "success", _FLASH_TAG_CREATED)
        return RedirectResponse(url="/tags", status_code=status.HTTP_303_SEE_OTHER)
    return JSONResponse(
        content={
            "tag": dto.model_dump(mode="json"),
            "applied_count": applied,
        },
        status_code=status.HTTP_201_CREATED,
    )


@api.get("/{tag_id}", response_model=TagDTO)
async def get_tag(
    db: DbSession,
    user: CurrentUser,
    tag_id: int = Path(..., ge=1),
) -> TagDTO:
    return await TagsService(db).get(user_id=user.id, tag_id=tag_id)


@api.patch("/{tag_id}", response_model=None)
async def update_tag(
    request: Request,
    db: DbSession,
    user: CurrentUser,
    tag_id: int = Path(..., ge=1),
) -> Response:
    user_id = user.id
    await consume(LIMIT_TAGS_WRITE, str(user_id))
    is_form = is_form_request(request)

    if is_form:
        payload = await _parse_update_form(request)
    else:
        body = await request.json()
        try:
            payload = TagUpdateRequest.model_validate(body)
        except PydanticValidationError as exc:
            raise ValidationError("Invalid JSON payload") from exc

    try:
        async with db.begin():
            dto = await TagsService(db).update(
                user_id=user_id,
                tag_id=tag_id,
                name=payload.name,
                color=payload.color,
                match_mode=payload.match_mode,
            )
    except DomainError as exc:
        if is_form:
            await flash(request, "error", exc.message)
            return RedirectResponse(
                url=f"/tags/{tag_id}/edit",
                status_code=status.HTTP_303_SEE_OTHER,
            )
        raise

    if is_form:
        await flash(request, "success", _FLASH_TAG_UPDATED)
        return RedirectResponse(url="/tags", status_code=status.HTTP_303_SEE_OTHER)
    return JSONResponse(content=dto.model_dump(mode="json"))


async def _delete_tag_impl(
    request: Request,
    db: DbSession,
    user: CurrentUser,
    tag_id: int,
) -> Response:
    """Shared body for canonical DELETE and sibling ``POST .../delete``."""
    user_id = user.id
    await consume(LIMIT_TAGS_WRITE, str(user_id))
    is_form = is_form_request(request)
    try:
        async with db.begin():
            await TagsService(db).delete(user_id=user_id, tag_id=tag_id)
    except DomainError as exc:
        if is_form:
            await flash(request, "error", exc.message)
            return RedirectResponse(url="/tags", status_code=status.HTTP_303_SEE_OTHER)
        raise
    if is_form:
        await flash(request, "success", _FLASH_TAG_DELETED)
        return RedirectResponse(url="/tags", status_code=status.HTTP_303_SEE_OTHER)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@api.delete("/{tag_id}", response_model=None)
async def delete_tag(
    request: Request,
    db: DbSession,
    user: CurrentUser,
    tag_id: int = Path(..., ge=1),
) -> Response:
    return await _delete_tag_impl(request, db, user, tag_id)


@api.delete(
    "/{tag_id}/delete",
    response_model=None,
    include_in_schema=False,
)
async def delete_tag_sibling(
    request: Request,
    db: DbSession,
    user: CurrentUser,
    tag_id: int = Path(..., ge=1),
) -> Response:
    """Sibling endpoint reached from a plain HTML form via method override."""
    return await _delete_tag_impl(request, db, user, tag_id)


# --- Rules CRUD -----------------------------------------------------------


@api.get("/{tag_id}/rules")
async def list_rules(
    db: DbSession,
    user: CurrentUser,
    tag_id: int = Path(..., ge=1),
) -> JSONResponse:
    """Return the rules for a single tag (ownership enforced)."""
    dto = await TagsService(db).get(user_id=user.id, tag_id=tag_id)
    return JSONResponse(content=[r.model_dump(mode="json") for r in dto.rules])


@api.post("/{tag_id}/rules", response_model=None, status_code=status.HTTP_201_CREATED)
async def add_rule(
    request: Request,
    db: DbSession,
    user: CurrentUser,
    tag_id: int = Path(..., ge=1),
) -> Response:
    user_id = user.id
    await consume(LIMIT_TAGS_WRITE, str(user_id))
    is_form = is_form_request(request)

    if is_form:
        form = await request.form()
        type_raw = _form_str(form, "type").strip()
        pattern_raw = _form_str(form, "pattern").strip()
        try:
            spec = RuleSpec(type=type_raw, pattern=pattern_raw)  # type: ignore[arg-type]
        except PydanticValidationError as exc:
            if is_form:
                await flash(request, "error", _FLASH_INVALID_RULE)
                return RedirectResponse(
                    url=f"/tags/{tag_id}/edit",
                    status_code=status.HTTP_303_SEE_OTHER,
                )
            raise ValidationError("Invalid rule") from exc
    else:
        body = await request.json()
        try:
            spec = RuleSpec.model_validate(body)
        except PydanticValidationError as exc:
            raise ValidationError("Invalid JSON payload") from exc

    try:
        async with db.begin():
            rule = await TagsService(db).add_rule(
                user_id=user_id,
                tag_id=tag_id,
                type_=spec.type,
                pattern=spec.pattern,
            )
    except DomainError as exc:
        if is_form:
            await flash(request, "error", exc.message)
            return RedirectResponse(
                url=f"/tags/{tag_id}/edit",
                status_code=status.HTTP_303_SEE_OTHER,
            )
        raise

    if is_form:
        await flash(request, "success", _FLASH_RULE_ADDED)
        return RedirectResponse(
            url=f"/tags/{tag_id}/edit",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    return JSONResponse(
        content=rule.model_dump(mode="json"),
        status_code=status.HTTP_201_CREATED,
    )


async def _delete_rule_impl(
    request: Request,
    db: DbSession,
    user: CurrentUser,
    tag_id: int,
    rule_id: int,
) -> Response:
    user_id = user.id
    await consume(LIMIT_TAGS_WRITE, str(user_id))
    is_form = is_form_request(request)
    try:
        async with db.begin():
            await TagsService(db).delete_rule(user_id=user_id, tag_id=tag_id, rule_id=rule_id)
    except DomainError as exc:
        if is_form:
            await flash(request, "error", exc.message)
            return RedirectResponse(
                url=f"/tags/{tag_id}/edit",
                status_code=status.HTTP_303_SEE_OTHER,
            )
        raise
    if is_form:
        await flash(request, "success", _FLASH_RULE_DELETED)
        return RedirectResponse(
            url=f"/tags/{tag_id}/edit",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@api.delete("/{tag_id}/rules/{rule_id}", response_model=None)
async def delete_rule(
    request: Request,
    db: DbSession,
    user: CurrentUser,
    tag_id: int = Path(..., ge=1),
    rule_id: int = Path(..., ge=1),
) -> Response:
    return await _delete_rule_impl(request, db, user, tag_id, rule_id)


@api.delete(
    "/{tag_id}/rules/{rule_id}/delete",
    response_model=None,
    include_in_schema=False,
)
async def delete_rule_sibling(
    request: Request,
    db: DbSession,
    user: CurrentUser,
    tag_id: int = Path(..., ge=1),
    rule_id: int = Path(..., ge=1),
) -> Response:
    return await _delete_rule_impl(request, db, user, tag_id, rule_id)


# --- Apply to existing -----------------------------------------------------


@api.post("/{tag_id}/apply-to-existing", response_model=None)
async def apply_to_existing(
    request: Request,
    db: DbSession,
    user: CurrentUser,
    tag_id: int = Path(..., ge=1),
) -> Response:
    user_id = user.id
    await consume(LIMIT_TAGS_APPLY, str(user_id))
    is_form = is_form_request(request)
    try:
        async with db.begin():
            applied = await TagsService(db).apply_to_existing(user_id=user_id, tag_id=tag_id)
    except DomainError as exc:
        if is_form:
            await flash(request, "error", exc.message)
            return RedirectResponse(url="/tags", status_code=status.HTTP_303_SEE_OTHER)
        raise
    if is_form:
        await flash(request, "success", _FLASH_APPLIED_N.format(n=applied))
        return RedirectResponse(url="/tags", status_code=status.HTTP_303_SEE_OTHER)
    return JSONResponse(content={"applied_count": applied})


# ---------------------------------------------------------------------------
# HTML pages
# ---------------------------------------------------------------------------


@html.get("/tags", response_class=HTMLResponse)
async def tags_list_page(request: Request, db: DbSession, user: CurrentUser) -> Response:
    sess = request.state.session
    tags = await TagsService(db).list_for_user(user.id)
    return await render(
        request,
        "tags/list.html",
        {
            "tags": tags,
            "csrf_token": sess.csrf_token,
            "session": sess,
        },
    )


@html.get("/tags/new", response_class=HTMLResponse)
async def tags_new_page(request: Request, user: CurrentUser) -> Response:
    sess = request.state.session
    _ = user  # auth via dependency
    return await render(
        request,
        "tags/form.html",
        {
            "tag": None,
            "form": {
                "name": "",
                "color": "",
                "match_mode": "any",
                "rules": [],
                "apply_to_existing": False,
            },
            "palette": sorted(PALETTE_COLORS),
            "csrf_token": sess.csrf_token,
            "session": sess,
            "is_edit": False,
        },
    )


@html.get("/tags/{tag_id}/edit", response_class=HTMLResponse)
async def tags_edit_page(
    request: Request,
    db: DbSession,
    user: CurrentUser,
    tag_id: int = Path(..., ge=1),
) -> Response:
    sess = request.state.session
    tag = await TagsService(db).get(user_id=user.id, tag_id=tag_id)
    return await render(
        request,
        "tags/form.html",
        {
            "tag": tag,
            "form": {
                "name": tag.name,
                "color": tag.color,
                "match_mode": tag.match_mode,
                "rules": [{"type": r.type, "pattern": r.pattern} for r in tag.rules],
                "apply_to_existing": False,
            },
            "palette": sorted(PALETTE_COLORS),
            "csrf_token": sess.csrf_token,
            "session": sess,
            "is_edit": True,
        },
    )


# Combined router export.
router = APIRouter()
router.include_router(api)
router.include_router(html)
