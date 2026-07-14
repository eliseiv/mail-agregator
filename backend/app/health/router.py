"""Health endpoints (ADR-0044 §4, phase A3 — KEEP-router detach).

- ``GET /healthz`` — liveness, no deps.
- ``GET /readyz``  — readiness: Postgres + Redis.

Removed together with the UI/sessions and MinIO (ADR-0043 §4 / ADR-0044 §4):
``GET /api/me`` and ``PATCH /api/me/settings`` (they read ``users.group_id``,
``groups``, ``telegram_links``, ``users_settings``) and the S3 probe in
``readyz``.
"""

from __future__ import annotations

from fastapi import APIRouter, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy import select

from backend.app.deps import DbSession
from shared.logging import get_logger
from shared.redis_client import get_redis

log = get_logger(__name__)
router = APIRouter()


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(db: DbSession) -> Response:
    db_ok = False
    redis_ok = False

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

    body = {
        "db": "ok" if db_ok else "fail",
        "redis": "ok" if redis_ok else "fail",
    }
    if not (db_ok and redis_ok):
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
