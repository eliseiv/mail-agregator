"""Health & self endpoints.

- ``GET /healthz`` — liveness, no deps.
- ``GET /readyz``  — readiness, checks Postgres + Redis + MinIO.
- ``GET /api/me``  — current user summary.
"""

from __future__ import annotations

from fastapi import APIRouter, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy import select

from backend.app.deps import CurrentUser, DbSession
from backend.app.repositories.mail_accounts import MailAccountsRepo
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
    return {
        "id": user.id,
        "username": user.username,
        "is_admin": user.is_admin,
        "last_login_at": user.last_login_at.isoformat() if user.last_login_at else None,
        "mail_accounts_count": len(accounts),
    }
