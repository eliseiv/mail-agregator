"""Integration-test fixtures.

- ``app`` / ``client`` — live FastAPI app behind ``httpx.AsyncClient``.
- ``_db_truncate_all`` — autouse, wipes every table (in dependency order)
  before each test so requests that open their own sessions don't bleed
  state between tests.
- ``_redis_flush`` — autouse, FLUSHDB before each test.

MinIO isolation is gone: attachments / ``shared.storage`` were removed in the
decommission (ADR-0044 phase G), so there is nothing to clean between tests.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.conftest import _pg_available, _redis_available

# The post-decommission schema (ADR-0044 phases C-F, revisions 025-028) keeps
# exactly three domain tables — everything else (tags/attachments/telegram/
# webhooks/groups/forwarding/admin_audit/sent_messages, 15 tables in phase D +
# ``groups`` in phase E) has been dropped. Order matters for TRUNCATE: child
# tables first (``RESTART IDENTITY CASCADE`` also covers any residual FK), so
# ``messages`` (→ ``mail_accounts``) precedes ``mail_accounts`` (→ ``users``)
# precedes ``users``. ``alembic_version`` is never truncated.
_TABLES_TRUNCATE_ORDER = [
    "messages",
    "mail_accounts",
    "users",
]


@pytest_asyncio.fixture(autouse=True)
async def _db_truncate_all(db_engine: AsyncEngine) -> AsyncIterator[None]:
    """Wipe every domain table + audit before each test.

    We deliberately TRUNCATE rather than DROP so the alembic migrations
    only ever run once. ``RESTART IDENTITY CASCADE`` resets sequences so
    ID-based assertions stay deterministic across tests.
    """
    if not _pg_available():
        pytest.skip("postgres not reachable")
    async with db_engine.begin() as conn:
        joined = ", ".join(_TABLES_TRUNCATE_ORDER)
        await conn.execute(text(f"TRUNCATE TABLE {joined} RESTART IDENTITY CASCADE"))
    yield


@pytest_asyncio.fixture(autouse=True)
async def _redis_flush() -> AsyncIterator[None]:
    if not _redis_available():
        pytest.skip("redis not reachable")
    from shared.redis_client import get_redis

    r = get_redis()
    await r.flushdb()
    yield
    await r.flushdb()


# ---------------------------------------------------------------------------
# App + HTTP client
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def app() -> AsyncIterator[Any]:
    """Build the FastAPI app for one test (full lifespan)."""
    if not (_pg_available() and _redis_available()):
        pytest.skip("integration deps missing")
    # Ensure the global engine is fresh — older lifespans may have disposed it.
    from shared.db import dispose_engine

    await dispose_engine()
    from backend.app.main import create_app

    application = create_app()
    # Manually run lifespan startup/shutdown since we don't use TestClient.
    async with application.router.lifespan_context(application):
        yield application


@pytest_asyncio.fixture
async def client(app: Any) -> AsyncIterator[httpx.AsyncClient]:
    """``httpx.AsyncClient`` bound directly to the ASGI app."""
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        follow_redirects=False,
    ) as c:
        yield c
