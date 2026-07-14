"""FastAPI application factory (headless connector, ADR-0044 §5).

Wired by ``deploy/Dockerfile`` (target=api): ``backend.app.main:app``.

After the decommission (ADR-0043 §4 / ADR-0044 §5) the app carries the machine
surface ONLY — the HTML UI, static files, cookie sessions, CSRF and the method
override are gone:

- ``external_router`` — ``/api/external/*`` (mailbox write/pull + headless
  Outlook OAuth, ADR-0045);
- ``health_router`` — ``/healthz`` / ``/readyz``.

Startup order:

1. Configure structlog (``service="api"``).
2. Initialise async DB engine.
3. ``seed_crm_service_user`` (idempotent) — the owner of every mailbox
   (ADR-0039).
4. Mount routers.

Shutdown: dispose engine, close Redis.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI

from backend.app.auth.service import seed_crm_service_user
from backend.app.exceptions import install_exception_handlers
from backend.app.external.router import router as external_router
from backend.app.health.router import router as health_router
from backend.app.middlewares import RequestIDMiddleware, SecurityHeadersMiddleware
from backend.app.rate_limit import install_rate_limiter
from shared.config import get_settings
from shared.db import dispose_engine, init_engine, make_session
from shared.logging import configure_logging, get_logger
from shared.redis_client import close_redis

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[dict[str, Any]]:
    settings = get_settings()
    configure_logging(level=settings.LOG_LEVEL, service="api")
    log.info("api_starting", env=settings.APP_ENV)

    # DB engine.
    init_engine(role="api")

    # ADR-0044 §5: the lifespan keeps ONLY the ``crm-service`` seed (the mailbox
    # owner). ``seed_super_admin`` / ``seed_builtin_tags`` / ``ensure_bucket``
    # went away with the UI / tags / MinIO.
    try:
        async with make_session() as s, s.begin():
            await seed_crm_service_user(s)
    except Exception as exc:  # — log + crash the boot loud
        log.error("crm_service_seed_failed", detail=str(exc)[:300])
        raise

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
    # Starlette runs middlewares in REVERSE add-order → add innermost first.
    # On the wire: RequestID -> SecurityHeaders -> route.
    # ADR-0044 §5: ``CSRFMiddleware`` / ``MethodOverrideMiddleware`` /
    # ``SessionMiddleware`` served the cookie UI and went away with it.
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RequestIDMiddleware)

    # --- Exception handlers + rate limiter ---
    install_exception_handlers(app)
    install_rate_limiter(app)

    # --- Routers (ADR-0044 §5: the machine surface only) ---
    app.include_router(external_router)
    app.include_router(health_router)

    return app


app = create_app()
