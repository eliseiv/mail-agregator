"""Pytest fixtures shared across all test packages.

Design:

- The fixtures here are deliberately defensive: integration fixtures
  ``db_engine``, ``db_session``, ``redis_client``, ``storage`` will *skip*
  the using test if their backing service isn't reachable. That keeps
  ``pytest tests/unit`` working without docker, and lets ``pytest
  tests/integration`` light up with the real stack via
  ``docker-compose.test.yml`` (see ``tests/README.md``).

- ``app`` and ``client`` use the real ASGI surface via
  ``httpx.AsyncClient(transport=ASGITransport(app=app))``. We do NOT use
  TestClient because middlewares-with-streamed-bodies are easier to debug
  through the async transport.

- DB isolation: the integration ``db_engine`` is shared (session-scoped),
  but each test that mutates DB does its work inside a function-scoped
  ``db_session`` that opens a transaction and rolls it back at teardown.
  For end-to-end tests through the API (which open their own sessions),
  we instead truncate-after-test via ``_db_truncate_all`` autouse fixture
  scoped to the integration package — see ``tests/integration/conftest.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

# ---------------------------------------------------------------------------
# Event loop policy — session-scoped so async clients survive across tests
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def event_loop_policy() -> asyncio.AbstractEventLoopPolicy:
    """pytest-asyncio reads this to choose its loop policy."""
    return asyncio.DefaultEventLoopPolicy()


# ---------------------------------------------------------------------------
# Settings — load .env once and expose for fixtures that need raw values.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def settings() -> Any:
    """Return the app settings singleton (loaded from .env)."""
    # ``shared.config.get_settings`` is lru_cached on the process; reuse it
    # so that whatever the app sees is what tests see.
    from shared.config import get_settings

    return get_settings()


# ---------------------------------------------------------------------------
# Helpers for graceful "is the service reachable on localhost?" checks
# ---------------------------------------------------------------------------


def _can_connect(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _parse_host_port(url: str, default_host: str, default_port: int) -> tuple[str, int]:
    """Best-effort host/port extraction (works for postgres, redis, http URLs)."""
    from urllib.parse import urlparse

    try:
        u = urlparse(url)
        host = u.hostname or default_host
        port = u.port or default_port
        return host, port
    except (ValueError, TypeError):
        return default_host, default_port


# Module-level cached availability flags so each test pays the TCP probe at
# most once per pytest session.
_PG_AVAILABLE: bool | None = None
_REDIS_AVAILABLE: bool | None = None
_S3_AVAILABLE: bool | None = None


def _pg_available() -> bool:
    global _PG_AVAILABLE
    if _PG_AVAILABLE is None:
        url = os.environ.get(
            "DATABASE_URL",
            "postgresql+asyncpg://mas:test_postgres_password_for_qa@127.0.0.1:55432/mail_aggregator",
        )
        host, port = _parse_host_port(url.replace("+asyncpg", ""), "127.0.0.1", 5432)
        _PG_AVAILABLE = _can_connect(host, port)
    return _PG_AVAILABLE


def _redis_available() -> bool:
    global _REDIS_AVAILABLE
    if _REDIS_AVAILABLE is None:
        url = os.environ.get("REDIS_URL", "redis://127.0.0.1:56379/0")
        host, port = _parse_host_port(url, "127.0.0.1", 6379)
        _REDIS_AVAILABLE = _can_connect(host, port)
    return _REDIS_AVAILABLE


def _s3_available() -> bool:
    global _S3_AVAILABLE
    if _S3_AVAILABLE is None:
        url = os.environ.get("S3_ENDPOINT_URL", "http://127.0.0.1:59000")
        host, port = _parse_host_port(url, "127.0.0.1", 9000)
        _S3_AVAILABLE = _can_connect(host, port)
    return _S3_AVAILABLE


# Expose as fixtures so individual tests can opt-in to their own skip logic.
@pytest.fixture
def pg_available() -> bool:
    return _pg_available()


@pytest.fixture
def redis_available() -> bool:
    return _redis_available()


@pytest.fixture
def s3_available() -> bool:
    return _s3_available()


# ---------------------------------------------------------------------------
# DB engine + session (integration only)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db_engine() -> AsyncIterator[AsyncEngine]:
    """Async engine pointed at the test Postgres. Skips if PG isn't up.

    Function-scoped: pytest-asyncio's per-test event loop closes between
    tests, and asyncpg connections cannot be reused across loops. Cheap
    enough at <50 ms per setup.
    """
    if not _pg_available():
        pytest.skip("postgres not reachable on test endpoint — start docker compose")
    from shared.config import get_settings

    s = get_settings()
    engine = create_async_engine(
        s.DATABASE_URL,
        echo=False,
        pool_pre_ping=True,
        future=True,
        # Tiny pool — each test gets its own engine.
        pool_size=2,
        max_overflow=2,
    )
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(db_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """Function-scoped session in a SAVEPOINT-rolled-back transaction.

    Use for unit-level repository tests. End-to-end tests that go through
    the API stack should use ``_db_truncate_all`` (autouse in integration
    package conftest) instead, because requests open their own sessions.
    """
    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as session:
        yield session
        # Roll back any uncommitted state and close.
        await session.rollback()


# ---------------------------------------------------------------------------
# Redis (integration)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def redis_client() -> AsyncIterator[Any]:
    """Async Redis client; FLUSHDBs before yielding to keep tests isolated."""
    if not _redis_available():
        pytest.skip("redis not reachable on test endpoint")
    from shared.redis_client import get_redis

    r = get_redis()
    await r.flushdb()
    yield r
    # Drain the DB so the next test starts clean. Closing the client itself
    # happens in the autouse ``_close_redis_after_each_test`` so we don't
    # leak a connection that's pinned to this test's event loop.
    await r.flushdb()


@pytest_asyncio.fixture(autouse=True)
async def _close_singletons_after_each_test() -> AsyncIterator[None]:
    """Close the singleton Redis + global async engine after every test.

    Both pin themselves to whichever asyncio loop they were first awaited
    on; pytest-asyncio creates a fresh loop per test, so the next test
    would hit ``Event loop is closed`` if we let them survive.
    """
    yield
    if _redis_available():
        from shared.redis_client import close_redis

        try:
            await close_redis()
        except Exception:  # — ignore if already closed
            pass
    # Dispose the shared.db global engine if anything created it.
    from shared import db as _shared_db

    if _shared_db._engine is not None:
        with contextlib.suppress(Exception):
            await _shared_db.dispose_engine()


# ---------------------------------------------------------------------------
# Storage (MinIO) — the ``storage`` fixture was removed together with
# ``shared/storage.py`` in the decommission (ADR-0044 phase G). Attachments /
# MinIO are gone from the domain, so no test handle to the bucket remains.
# The ``_s3_available`` probe above is kept only because the integration
# ``app`` / ``oauth_app`` fixtures still gate on the (CI-provisioned) MinIO
# service being reachable.
# ---------------------------------------------------------------------------
