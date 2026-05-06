"""Alembic env (async-first).

Uses the asyncpg-backed engine from :mod:`shared.db` so we don't need a
sync psycopg2/psycopg installed in the runtime image. Alembic's
``run_sync`` bridge runs the actual migrations on a sync connection
projected from the async one.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

import shared.models  # registers all ORM tables on Base.metadata; required side-effect
from shared.config import get_settings
from shared.db import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def _async_database_url() -> str:
    return get_settings().DATABASE_URL


# Expose the URL to alembic.ini consumers (mostly for `alembic` CLI logging).
config.set_main_option("sqlalchemy.url", _async_database_url())

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Generate SQL without an actual DB connection.

    Offline mode is rarely used in production deployments; we still wire it
    up so ``alembic upgrade head --sql`` works for review.
    """
    context.configure(
        url=_async_database_url().replace("postgresql+asyncpg://", "postgresql://"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Run migrations against an async asyncpg engine."""
    connectable = create_async_engine(_async_database_url(), poolclass=None)
    async with connectable.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
