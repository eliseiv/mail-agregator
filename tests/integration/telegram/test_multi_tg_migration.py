"""ADR-0024 §8 (Sprint A) — migration ``20260527_017`` round-trip (item A).

Runs the migration against a DEDICATED scratch database
(``mail_aggregator_migtest``) on the same test Postgres so the shared
integration DB (already at head) is never disturbed. The scratch DB is
created/dropped per test class.

Verified:

- ``alembic upgrade 20260521_016`` then ``upgrade 20260527_017`` applies
  cleanly;
- after upgrade, ``UNIQUE(user_id)`` is GONE from ``telegram_links`` and the
  non-unique ``telegram_links_user_id_idx`` remains; ``telegram_notifications``
  has a NOT-NULL ``telegram_user_id`` and the
  ``telegram_notifications_msg_chat_uq`` UNIQUE (and the old
  ``telegram_notifications_unique`` is gone);
- backfill: a notification of a user WITH a live link gets that link's
  ``telegram_user_id``; an ORPHANED notification (link already deleted) gets
  the synthetic ``0`` (TD-028);
- ``downgrade`` round-trip: restores ``(message_id, user_id)`` UNIQUE after a
  lossy dedup (multi-chat rows collapse to the earliest row).

The scratch DB is provisioned by connecting to the ``postgres`` maintenance DB
and issuing ``CREATE DATABASE`` (asyncpg, autocommit), then alembic is driven
via ``subprocess`` with ``DATABASE_URL`` pointed at the scratch DB.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import AsyncIterator
from urllib.parse import urlparse, urlunparse

import asyncpg
import pytest
import pytest_asyncio

from shared.config import get_settings
from tests.conftest import _pg_available

pytestmark = pytest.mark.integration

_SCRATCH_DB = "mail_aggregator_migtest"


def _asyncpg_dsn(database: str) -> str:
    """Build a plain (non-SQLAlchemy) asyncpg DSN for ``database`` from the
    configured ``DATABASE_URL`` (strip the ``+asyncpg`` driver tag)."""
    url = get_settings().DATABASE_URL.replace("+asyncpg", "")
    parts = urlparse(url)
    return urlunparse(parts._replace(path=f"/{database}"))


def _alembic_database_url(database: str) -> str:
    """SQLAlchemy URL (with ``+asyncpg``) pointed at ``database``."""
    url = get_settings().DATABASE_URL
    parts = urlparse(url)
    return urlunparse(parts._replace(path=f"/{database}"))


async def _drop_create_scratch() -> None:
    admin = await asyncpg.connect(dsn=_asyncpg_dsn("postgres"))
    try:
        # Terminate stray backends then recreate clean.
        await admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = $1 AND pid <> pg_backend_pid()",
            _SCRATCH_DB,
        )
        await admin.execute(f'DROP DATABASE IF EXISTS "{_SCRATCH_DB}"')
        await admin.execute(f'CREATE DATABASE "{_SCRATCH_DB}"')
    finally:
        await admin.close()


async def _drop_scratch() -> None:
    admin = await asyncpg.connect(dsn=_asyncpg_dsn("postgres"))
    try:
        await admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = $1 AND pid <> pg_backend_pid()",
            _SCRATCH_DB,
        )
        await admin.execute(f'DROP DATABASE IF EXISTS "{_SCRATCH_DB}"')
    finally:
        await admin.close()


def _alembic(*args: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["DATABASE_URL"] = _alembic_database_url(_SCRATCH_DB)
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest_asyncio.fixture
async def scratch_db() -> AsyncIterator[str]:
    if not _pg_available():
        pytest.skip("postgres not reachable")
    await _drop_create_scratch()
    try:
        yield _SCRATCH_DB
    finally:
        await _drop_scratch()


class TestMigration017:
    async def test_upgrade_to_016_then_017_clean(self, scratch_db: str) -> None:
        """Upgrade to the predecessor (016) then to 017 — both succeed."""
        r16 = _alembic("upgrade", "20260521_016")
        assert r16.returncode == 0, f"upgrade to 016 failed:\n{r16.stdout}\n{r16.stderr}"
        r17 = _alembic("upgrade", "20260527_017")
        assert r17.returncode == 0, f"upgrade to 017 failed:\n{r17.stdout}\n{r17.stderr}"

        conn = await asyncpg.connect(dsn=_asyncpg_dsn(scratch_db))
        try:
            # UNIQUE(user_id) gone from telegram_links.
            uq_user = await conn.fetchval(
                """
                SELECT COUNT(*) FROM pg_constraint
                WHERE conrelid = 'telegram_links'::regclass
                  AND contype = 'u'
                  AND conname = 'telegram_links_user_id_key'
                """
            )
            assert uq_user == 0, "UNIQUE(user_id) must be dropped on telegram_links"

            # Non-unique helper index still present.
            idx = await conn.fetchval(
                "SELECT COUNT(*) FROM pg_indexes WHERE tablename = 'telegram_links' "
                "AND indexname = 'telegram_links_user_id_idx'"
            )
            assert idx == 1, "telegram_links_user_id_idx must remain"

            # telegram_user_id column is NOT NULL on telegram_notifications.
            is_nullable = await conn.fetchval(
                """
                SELECT is_nullable FROM information_schema.columns
                WHERE table_name = 'telegram_notifications'
                  AND column_name = 'telegram_user_id'
                """
            )
            assert is_nullable == "NO", "telegram_user_id must be NOT NULL"

            # New per-chat UNIQUE present; old per-user UNIQUE gone.
            new_uq = await conn.fetchval(
                "SELECT COUNT(*) FROM pg_constraint "
                "WHERE conname = 'telegram_notifications_msg_chat_uq'"
            )
            assert new_uq == 1, "telegram_notifications_msg_chat_uq must exist"
            old_uq = await conn.fetchval(
                "SELECT COUNT(*) FROM pg_constraint "
                "WHERE conname = 'telegram_notifications_unique'"
            )
            assert old_uq == 0, "old (message_id, user_id) UNIQUE must be dropped"
        finally:
            await conn.close()

    async def test_backfill_live_link_and_orphan(self, scratch_db: str) -> None:
        """Seed pre-017 (at 016) data, run 017, assert backfill values."""
        assert _alembic("upgrade", "20260521_016").returncode == 0

        conn = await asyncpg.connect(dsn=_asyncpg_dsn(scratch_db))
        try:
            # Minimal rows: two users, one link for user A (live), none for B.
            await conn.execute(
                "INSERT INTO users (id, username, role, password_reset_required) "
                "VALUES (1, 'ua', 'super_admin', false), (2, 'ub', 'super_admin', false)"
            )
            await conn.execute(
                "INSERT INTO telegram_links (telegram_user_id, user_id, created_at) "
                "VALUES (5001, 1, now())"
            )
            # A mail account + two messages so notifications have valid FKs.
            await conn.execute(
                "INSERT INTO mail_accounts "
                "(id, user_id, email, encrypted_password, imap_host, imap_port, imap_ssl, "
                " smtp_host, smtp_port, smtp_ssl, smtp_starttls) "
                "VALUES (1, 1, 'a@x.com', '\\x00', 'imap', 993, true, 'smtp', 465, true, false)"
            )
            await conn.execute(
                "INSERT INTO messages "
                "(id, mail_account_id, uid, uidvalidity, from_addr, to_addrs, internal_date, "
                " body_text) "
                "VALUES (1, 1, 1, 1, 'f@x.com', 't@x.com', now(), ''), "
                "       (2, 1, 2, 1, 'f@x.com', 't@x.com', now(), '')"
            )
            # Notification for user A (HAS a link) and user B (ORPHAN — no link).
            await conn.execute(
                "INSERT INTO telegram_notifications (id, message_id, user_id, sent_at) "
                "VALUES (1, 1, 1, now()), (2, 2, 2, now())"
            )
        finally:
            await conn.close()

        assert _alembic("upgrade", "20260527_017").returncode == 0

        conn = await asyncpg.connect(dsn=_asyncpg_dsn(scratch_db))
        try:
            # User A's notification → its live link's chat (5001).
            tg_a = await conn.fetchval(
                "SELECT telegram_user_id FROM telegram_notifications WHERE id = 1"
            )
            assert tg_a == 5001, "backfill must use the user's live link chat"
            # User B's orphan notification → synthetic 0 (TD-028).
            tg_b = await conn.fetchval(
                "SELECT telegram_user_id FROM telegram_notifications WHERE id = 2"
            )
            assert tg_b == 0, "orphaned notification must backfill to synthetic 0"
        finally:
            await conn.close()

    async def test_downgrade_round_trip_lossy_dedup(self, scratch_db: str) -> None:
        """Upgrade to 017, create a multi-chat collision, downgrade → the
        ``(message_id, user_id)`` UNIQUE is restored and duplicates collapse to
        the earliest row (documented lossy dedup)."""
        assert _alembic("upgrade", "20260527_017").returncode == 0

        conn = await asyncpg.connect(dsn=_asyncpg_dsn(scratch_db))
        try:
            await conn.execute(
                "INSERT INTO users (id, username, role, password_reset_required) "
                "VALUES (1, 'ua', 'super_admin', false)"
            )
            # Distinct created_at so the downgrade dedup (which tiebreaks on
            # created_at) can deterministically keep the earliest link. NB: the
            # downgrade's ``a.created_at > b.created_at`` dedup does NOT collapse
            # links that share an identical timestamp — see the QA finding on
            # equal-timestamp fragility; production links are created at distinct
            # times so this is the realistic round-trip.
            await conn.execute(
                "INSERT INTO telegram_links (telegram_user_id, user_id, created_at) "
                "VALUES (6001, 1, now() - interval '1 hour'), (6002, 1, now())"
            )
            await conn.execute(
                "INSERT INTO mail_accounts "
                "(id, user_id, email, encrypted_password, imap_host, imap_port, imap_ssl, "
                " smtp_host, smtp_port, smtp_ssl, smtp_starttls) "
                "VALUES (1, 1, 'a@x.com', '\\x00', 'imap', 993, true, 'smtp', 465, true, false)"
            )
            await conn.execute(
                "INSERT INTO messages "
                "(id, mail_account_id, uid, uidvalidity, from_addr, to_addrs, internal_date, "
                " body_text) "
                "VALUES (1, 1, 1, 1, 'f@x.com', 't@x.com', now(), '')"
            )
            # Two rows for the SAME (message_id=1, user_id=1) but different chats
            # — only possible under the new per-chat key.
            await conn.execute(
                "INSERT INTO telegram_notifications "
                "(id, message_id, user_id, telegram_user_id, sent_at) "
                "VALUES (10, 1, 1, 6001, now()), (11, 1, 1, 6002, now())"
            )
        finally:
            await conn.close()

        r = _alembic("downgrade", "20260521_016")
        assert r.returncode == 0, f"downgrade failed:\n{r.stdout}\n{r.stderr}"

        conn = await asyncpg.connect(dsn=_asyncpg_dsn(scratch_db))
        try:
            # Old UNIQUE restored.
            old_uq = await conn.fetchval(
                "SELECT COUNT(*) FROM pg_constraint "
                "WHERE conname = 'telegram_notifications_unique'"
            )
            assert old_uq == 1, "downgrade must restore (message_id, user_id) UNIQUE"
            # Lossy dedup kept the earliest row (MIN id = 10), dropped 11.
            remaining = await conn.fetch(
                "SELECT id FROM telegram_notifications WHERE message_id = 1 AND user_id = 1"
            )
            ids = sorted(row["id"] for row in remaining)
            assert ids == [10], f"lossy dedup must keep earliest row only, got {ids}"
            # telegram_user_id column dropped.
            col = await conn.fetchval(
                "SELECT COUNT(*) FROM information_schema.columns "
                "WHERE table_name = 'telegram_notifications' AND column_name = 'telegram_user_id'"
            )
            assert col == 0, "downgrade must drop the telegram_user_id column"
        finally:
            await conn.close()
