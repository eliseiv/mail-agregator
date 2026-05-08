"""FastAPI application factory.

Wired by ``deploy/Dockerfile`` (target=api): ``backend.app.main:app``.

Startup order:

1. Configure structlog (``service="api"``).
2. Initialise async DB engine.
3. Run ``seed_super_admin`` (idempotent UPSERT).
4. ``Storage.ensure_bucket`` defensive check (init container also creates).
5. Mount routers.

Shutdown:

1. Dispose engine.
2. Close Redis client.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from backend.app.accounts.router import router as accounts_router
from backend.app.admin.router import router as admin_router
from backend.app.auth.router import router as auth_router
from backend.app.auth.service import seed_super_admin
from backend.app.csrf import CSRFMiddleware
from backend.app.exceptions import (
    NotAuthenticatedError,
    _domain_handler,
    install_exception_handlers,
)
from backend.app.groups.router import router as groups_router
from backend.app.health.router import router as health_router
from backend.app.messages.router import router as messages_router
from backend.app.middlewares import (
    MethodOverrideMiddleware,
    RequestIDMiddleware,
    SecurityHeadersMiddleware,
    SessionMiddleware,
)
from backend.app.rate_limit import install_rate_limiter
from backend.app.send.router import router as send_router
from backend.app.tags.router import router as tags_router
from backend.app.telegram.router import router as telegram_router
from shared.config import get_settings
from shared.db import dispose_engine, init_engine, make_session
from shared.logging import configure_logging, get_logger
from shared.redis_client import close_redis
from shared.storage import get_storage

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[dict[str, Any]]:
    settings = get_settings()
    configure_logging(level=settings.LOG_LEVEL, service="api")
    log.info("api_starting", env=settings.APP_ENV)

    # DB engine.
    init_engine(role="api")

    # Seed super-admin (idempotent).
    try:
        async with make_session() as s, s.begin():
            await seed_super_admin(s)
    except Exception as exc:  # — log + crash the boot loud
        log.error("admin_seed_failed", detail=str(exc)[:300])
        raise

    # Defensive: ensure_bucket (init container should have done it already).
    try:
        await get_storage().ensure_bucket()
    except Exception as exc:
        # Don't fail startup — health/readyz will surface this.
        log.warning("ensure_bucket_failed_at_startup", detail=str(exc)[:300])

    log.info("api_started")
    try:
        yield {}
    finally:
        log.info("api_stopping")
        await dispose_engine()
        await close_redis()
        log.info("api_stopped")


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Mail Aggregator API",
        version="0.1.0",
        docs_url="/docs" if settings.ENABLE_DOCS else None,
        redoc_url=None,
        openapi_url="/openapi.json",
        lifespan=lifespan,
    )

    # --- Middlewares (order matters; outermost first) ---
    # FastAPI/Starlette runs middlewares in REVERSE add-order, so add
    # innermost first. We want the order on the wire to be:
    #   RequestID -> SecurityHeaders -> Session -> MethodOverride -> CSRF -> route
    # MethodOverride must precede CSRF so the CSRF check sees the effective
    # method (DELETE/PATCH after override). See ADR-0015.
    app.add_middleware(CSRFMiddleware)
    app.add_middleware(MethodOverrideMiddleware)
    app.add_middleware(SessionMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RequestIDMiddleware)

    # --- Static files / templates -------------------------------------------------
    # Frontend agent populates ``static/`` and ``templates/``. We do NOT
    # silently create empty dirs in prod — a misconfigured deploy that ships
    # without static assets must fail fast; in dev we just warn so the
    # app remains bootable while the frontend agent is iterating.
    static_dir = Path(__file__).parent / "static"
    templates_dir = Path(__file__).parent / "templates"

    def _dir_is_empty(path: Path) -> bool:
        return not path.is_dir() or not any(path.iterdir())

    if settings.is_prod:
        if _dir_is_empty(static_dir):
            raise RuntimeError(
                f"static directory {static_dir} is missing or empty in prod — "
                "frontend assets were not built into the image"
            )
        if _dir_is_empty(templates_dir):
            raise RuntimeError(
                f"templates directory {templates_dir} is missing or empty in prod — "
                "frontend templates were not built into the image"
            )
    else:
        # Dev: create + warn so a fresh checkout still boots even when the
        # frontend agent hasn't generated static yet.
        static_dir.mkdir(parents=True, exist_ok=True)
        templates_dir.mkdir(parents=True, exist_ok=True)
        if _dir_is_empty(static_dir):
            log.warning("static_dir_empty", path=str(static_dir))
        if _dir_is_empty(templates_dir):
            log.warning("templates_dir_empty", path=str(templates_dir))

    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # --- Exception handlers + rate limiter ---
    install_exception_handlers(app)
    install_rate_limiter(app)

    # --- Routers ---
    app.include_router(auth_router)
    app.include_router(accounts_router)
    app.include_router(messages_router)
    app.include_router(send_router)
    app.include_router(tags_router)
    app.include_router(admin_router)
    app.include_router(groups_router)
    app.include_router(telegram_router)
    app.include_router(health_router)

    # --- Friendly redirects for HTML pages when not authenticated ---
    # API routes return 401 JSON. HTML routes redirect to /login.
    @app.exception_handler(NotAuthenticatedError)
    async def _not_auth_html_handler(request: Request, exc: NotAuthenticatedError) -> Any:
        if request.url.path.startswith("/api/"):
            return _domain_handler(request, exc)
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)

    return app


app = create_app()
