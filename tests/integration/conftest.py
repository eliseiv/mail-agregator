"""Integration-test fixtures.

- ``app`` / ``client`` — live FastAPI app behind ``httpx.AsyncClient``.
- ``_db_truncate_all`` — autouse, wipes every table (in dependency order)
  before each test so requests that open their own sessions don't bleed
  state between tests.
- ``_redis_flush`` — autouse, FLUSHDB before each test.
- ``_minio_clean`` — autouse, deletes every object under the test prefix
  so attachment uploads from previous tests don't bleed.
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

from tests.conftest import _pg_available, _redis_available, _s3_available

# Order matters for TRUNCATE: child tables first (or use CASCADE).
_TABLES_TRUNCATE_ORDER = [
    "sent_attachments",
    "sent_messages",
    "attachments",
    "messages",
    "mail_accounts",
    "admin_audit",
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


@pytest_asyncio.fixture(autouse=True)
async def _minio_clean() -> AsyncIterator[None]:
    """Clear the bucket before each test.

    MinIO is shared across tests; without cleanup, previous attachments
    pile up under ``user_id/account_id/...`` prefixes. We run delete-all
    by paginating list+delete (handled by ``Storage.delete_prefix``).
    """
    if not _s3_available():
        pytest.skip("minio not reachable")
    from shared.storage import get_storage

    st = get_storage()
    # Ensure bucket on first request — cheap idempotent.
    await st.ensure_bucket()
    # Empty bucket is just an empty pagination list; this is a no-op.
    # We use a single-character prefix that sweeps the whole namespace.
    # (delete_prefix rejects empty prefix.)
    for prefix in "0123456789":
        await st.delete_prefix(prefix)
    yield


# ---------------------------------------------------------------------------
# App + HTTP client
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def app() -> AsyncIterator[Any]:
    """Build the FastAPI app for one test (full lifespan)."""
    if not (_pg_available() and _redis_available() and _s3_available()):
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


# ---------------------------------------------------------------------------
# Helper: log in a user and return the client with cookies set + csrf token.
# ---------------------------------------------------------------------------


async def two_step_login(
    client: httpx.AsyncClient, username: str, password: str
) -> httpx.Response:
    """Drive the two-step login flow (ADR-0016) on ``client``.

    Returns the step-2 response so callers can grab cookies (``mas_session``,
    ``mas_csrf``) or assert on status. On the success path the response is a
    303 redirect to ``/`` with the session cookies set.
    """
    r1 = await client.post(
        "/login",
        data={"username": username},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert r1.status_code in (
        302,
        303,
    ), f"step1 expected redirect, got {r1.status_code}: {r1.text[:200]}"
    r2 = await client.post(
        "/login/password",
        data={"password": password},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    return r2


@pytest_asyncio.fixture
async def login_as(client: httpx.AsyncClient):
    """Login helper fixture that drives the two-step flow.

    Usage in a test::

        async def test_x(client, login_as):
            csrf = await login_as("admin", "secret")
            # client now has mas_session + mas_csrf cookies set.

    Returns the CSRF token on success. Asserts the success path; tests that
    need to inspect failure responses should call :func:`two_step_login`
    directly.
    """

    async def _login(username: str, password: str) -> str:
        resp = await two_step_login(client, username, password)
        assert resp.status_code in (
            302,
            303,
        ), f"step2 expected redirect, got {resp.status_code}: {resp.text[:200]}"
        csrf = resp.cookies.get("mas_csrf")
        assert csrf, "csrf cookie not set on login redirect"
        return csrf

    return _login


async def login_as_admin(client: httpx.AsyncClient) -> str:
    """Log in as the seeded super-admin. Returns the CSRF token.

    Drives the two-step login flow (ADR-0016).
    """
    from shared.config import get_settings

    s = get_settings()
    resp = await two_step_login(client, s.ADMIN_LOGIN, s.ADMIN_PASSWORD)
    assert resp.status_code in (302, 303), resp.text
    csrf = resp.cookies.get("mas_csrf")
    assert csrf, "csrf cookie not set on login redirect"
    return csrf
