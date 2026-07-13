"""Async SQLAlchemy 2.x engine + session factory.

Two engines: ``api`` uses the default (pool_size=10, max_overflow=20),
``worker`` uses a smaller pool (pool_size=5). Both connect via asyncpg.

Sessions:
- API uses :func:`get_session` as a FastAPI dependency.
- Worker calls :func:`make_session` directly inside scheduled jobs.

Invariants (``docs/05-modules.md`` sec. 2):
- Sessions always closed via ``async with`` or dependency cleanup.
- Mutations happen inside ``async with session.begin()`` (explicit txn).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from shared.config import Settings, get_settings
from shared.session_guards import check_session_guards


class Base(DeclarativeBase):
    """Declarative base class for all ORM models in :mod:`shared.models`."""


_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def _build_engine(settings: Settings, role: Literal["api", "worker"]) -> AsyncEngine:
    pool_size = 5 if role == "worker" else 10
    max_overflow = 5 if role == "worker" else 20
    return create_async_engine(
        settings.DATABASE_URL,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_pre_ping=True,
        pool_recycle=1800,  # 30 min — many cloud PG drops idle conns at 1h
        future=True,
        echo=False,  # never echo SQL — would leak parameters into logs
    )


def init_engine(role: Literal["api", "worker"] = "api") -> AsyncEngine:
    """Build the global engine + session factory. Called once at startup."""
    global _engine, _session_factory
    if _engine is None:
        _engine = _build_engine(get_settings(), role)
        _session_factory = async_sessionmaker(
            bind=_engine,
            expire_on_commit=False,
            class_=AsyncSession,
        )
    return _engine


def get_engine() -> AsyncEngine:
    if _engine is None:
        return init_engine()
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    if _session_factory is None:
        init_engine()
    assert _session_factory is not None
    return _session_factory


async def dispose_engine() -> None:
    """Tear down the engine — used in shutdown hooks and tests."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None


# --- Session helpers --------------------------------------------------------


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency. Yields a session and closes it on exit.

    No implicit transaction here — call sites open ``async with session.begin():``
    when they need a write transaction. Read-only queries can run without
    an explicit transaction (autocommit per statement).
    """
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
        finally:
            # TD-054 / ADR-0046 §2.1.1 — detect (never repair) deferred
            # post-COMMIT side effects the caller forgot to flush. Warning in
            # prod (the already-committed request still returns 200), hard fail
            # under pytest. Runs BEFORE close() so the pending state is intact.
            try:
                check_session_guards(session)
            finally:
                await session.close()


@asynccontextmanager
async def make_session() -> AsyncIterator[AsyncSession]:
    """Context manager for non-FastAPI callers (worker jobs)."""
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
        finally:
            # TD-054 — same detector for the non-HTTP callers (worker job, CLI,
            # script): the norm of ADR-0046 §2.1.1 is addressed to the CALLER as
            # such, not to "a file named router.py".
            try:
                check_session_guards(session)
            finally:
                await session.close()
