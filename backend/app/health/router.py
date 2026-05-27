"""Health & self endpoints.

- ``GET /healthz``           — liveness, no deps.
- ``GET /readyz``            — readiness, checks Postgres + Redis + MinIO.
- ``GET /api/me``            — current user summary (role + group + telegram).
- ``PATCH /api/me/settings`` — user preferences (ADR-0022 §2.7).
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import select

from backend.app.deps import CurrentUser, DbSession
from backend.app.exceptions import ValidationError as DomainValidationError
from backend.app.repositories.groups import GroupsRepo
from backend.app.repositories.mail_accounts import MailAccountsRepo
from backend.app.repositories.telegram_links import TelegramLinksRepo
from backend.app.repositories.user_settings import UserSettingsRepo
from shared.logging import get_logger
from shared.redis_client import get_redis
from shared.storage import get_storage

log = get_logger(__name__)
router = APIRouter()


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(db: DbSession) -> Response:
    db_ok = False
    redis_ok = False
    s3_ok = False

    try:
        await db.execute(select(1))
        db_ok = True
    except Exception as exc:
        log.warning("readyz_db_fail", detail=str(exc)[:200])

    try:
        pong = await get_redis().ping()
        redis_ok = bool(pong)
    except Exception as exc:
        log.warning("readyz_redis_fail", detail=str(exc)[:200])

    try:
        s3_ok = await get_storage().health_check()
    except Exception as exc:
        log.warning("readyz_s3_fail", detail=str(exc)[:200])

    body = {
        "db": "ok" if db_ok else "fail",
        "redis": "ok" if redis_ok else "fail",
        "s3": "ok" if s3_ok else "fail",
    }
    if not (db_ok and redis_ok and s3_ok):
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "error": {
                    "code": "dependency_unavailable",
                    "message": "One or more dependencies are unhealthy",
                    "details": body,
                }
            },
        )
    return JSONResponse(status_code=status.HTTP_200_OK, content=body)


@router.get("/api/me")
async def me(db: DbSession, user: CurrentUser) -> dict[str, object]:
    accounts = await MailAccountsRepo(db).list_for_user(user.id)
    group_brief: dict[str, object] | None = None
    if user.group_id is not None:
        group = await GroupsRepo(db).get_by_id(user.group_id)
        if group is not None:
            group_brief = {"id": group.id, "name": group.name}
    # ADR-0022 §2.7 + ADR-0024 — surface preferences + linkage status.
    tg_enabled = await UserSettingsRepo(db).get_tg_notifications_enabled(user.id)
    active_links = await TelegramLinksRepo(db).list_active_by_user_id(user.id)
    telegram_links_count = len(active_links)
    return {
        "id": user.id,
        "username": user.username,
        "display_name": user.display_name,
        "role": user.role,
        "group": group_brief,
        "last_login_at": user.last_login_at.isoformat() if user.last_login_at else None,
        "mail_accounts_count": len(accounts),
        "tg_notifications_enabled": tg_enabled,
        # ADR-0024: ``telegram_linked`` = at least one live link;
        # ``telegram_links_count`` = number of live links (list via
        # GET /api/telegram/links).
        "telegram_linked": telegram_links_count > 0,
        "telegram_links_count": telegram_links_count,
    }


# ---------------------------------------------------------------------------
# PATCH /api/me/settings (ADR-0022 §2.7)
# ---------------------------------------------------------------------------


class _MeSettingsPatch(BaseModel):
    """``PATCH /api/me/settings`` body.

    All fields are optional — empty body is rejected at the service layer
    (``validation_error``). On this iteration only
    ``tg_notifications_enabled`` is supported; future preferences become
    additional optional fields here.
    """

    tg_notifications_enabled: bool | None = None

    model_config = ConfigDict(extra="forbid")


@router.patch("/api/me/settings")
async def patch_me_settings(request: Request, db: DbSession, user: CurrentUser) -> Response:
    try:
        body = await request.json()
    except ValueError as exc:
        raise DomainValidationError("Body is not valid JSON") from exc
    try:
        payload = _MeSettingsPatch.model_validate(body)
    except ValidationError as exc:
        raise DomainValidationError("Invalid settings payload") from exc

    if payload.tg_notifications_enabled is None:
        raise DomainValidationError(
            "At least one settings field is required",
            field="tg_notifications_enabled",
        )

    settings_repo = UserSettingsRepo(db)

    old_value = await settings_repo.get_tg_notifications_enabled(user.id)
    new_value = bool(payload.tg_notifications_enabled)

    # Close the autobegun read-tx so the explicit begin() below does not collide.
    await db.commit()
    async with db.begin():
        row = await settings_repo.upsert_tg_notifications_enabled(
            user_id=user.id, enabled=new_value
        )

    if old_value != new_value:
        # ADR-0022 §2.7: preference changes are not an admin-audit event
        # (the audit log table is reserved for super-admin actions per
        # ``docs/03-data-model.md``). We still emit a structured log line so
        # operators can correlate "user X stopped receiving notifications"
        # with their own toggle.
        log.info(
            "tg_notifications_setting_changed",
            user_id=user.id,
            from_value=old_value,
            to_value=new_value,
        )

    return JSONResponse(
        content={"tg_notifications_enabled": bool(row.tg_notifications_enabled)},
        status_code=status.HTTP_200_OK,
    )
