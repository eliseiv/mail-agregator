"""ADR-0026 update §2 — transient suppression end-to-end (live PG).

Scope E of the QA task. Drives the REAL ``sync_cycle._handle_sync_error`` path
(which classifies via rule 3b, evaluates :func:`_should_suppress_transient`, and
conditionally writes ``last_sync_error`` via the no-bump repo method) against a
real Postgres row, for the sporadic Microsoft Outlook IMAP flake:

* TRANSIENT flake + FRESH ``last_synced_at`` (< window) -> ``mark_transient_error``
  is NOT called: ``last_sync_error`` is left untouched (suppressed in the UI), and
  the WARNING event records ``last_sync_error_suppressed=True``.
* TRANSIENT flake + STALE ``last_synced_at`` (> window) -> the error IS written.
* TRANSIENT flake + NULL ``last_synced_at`` (never synced) -> the error IS written.
* ``consecutive_failures`` is NEVER bumped for a transient, in every case, and the
  account stays active.

Needs a real Postgres (docker-compose.test.yml); reuses the worker package's
autouse DB/Redis/MinIO truncation fixtures.
"""

from __future__ import annotations

import datetime as _dt
import imaplib
from typing import Any

import pytest
import structlog
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from shared.crypto import encrypt_mail_password
from shared.models import MailAccount, User
from worker.app import sync_cycle as sc

pytestmark = pytest.mark.integration  # needs the DB to be live

# The canonical sporadic flake (rule 3b -> transient/network).
_FLAKE = imaplib.IMAP4.error("User is authenticated but not connected")


@pytest.fixture
async def seeded_account(db_engine: AsyncEngine) -> dict[str, Any]:
    """Seed a super-admin + one active mail_account; return ids."""
    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with factory() as ses, ses.begin():
        admin = User(
            username="suppress_admin",
            role="super_admin",
            password_hash="$argon2id$v=19$m=65536,t=3,p=4$abc$def",
            password_reset_required=False,
        )
        ses.add(admin)
        await ses.flush()
        from backend.app.repositories.mail_accounts import MailAccountsRepo

        new_id = await MailAccountsRepo(ses).next_account_id()
        acc = MailAccount(
            id=new_id,
            user_id=admin.id,
            email="suppress@example.com",
            encrypted_password=encrypt_mail_password("p", new_id),
            imap_host="imap.example.com",
            imap_port=993,
            imap_ssl=True,
            smtp_host="smtp.example.com",
            smtp_port=465,
            smtp_ssl=True,
            smtp_starttls=False,
        )
        ses.add(acc)
        await ses.flush()
        return {"user_id": admin.id, "account_id": acc.id}


async def _reload(db_engine: AsyncEngine, account_id: int) -> MailAccount:
    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with factory() as ses:
        acc = await ses.get(MailAccount, account_id)
    assert acc is not None
    return acc


async def _set_last_synced_at(
    db_engine: AsyncEngine, account_id: int, when: _dt.datetime | None
) -> None:
    from backend.app.repositories.mail_accounts import MailAccountsRepo

    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with factory() as ses, ses.begin():
        await MailAccountsRepo(ses).update_fields(account_id, last_synced_at=when)


def _force_window(monkeypatch: pytest.MonkeyPatch, minutes: int) -> None:
    """Force SYNC_TRANSIENT_SUPPRESS_MINUTES inside sync_cycle only (the rest of
    settings — used by repos/audit — stays the real .env value)."""
    real = sc.get_settings()

    class _S:
        def __getattr__(self, name: str) -> Any:
            if name == "SYNC_TRANSIENT_SUPPRESS_MINUTES":
                return minutes
            return getattr(real, name)

    monkeypatch.setattr(sc, "get_settings", lambda: _S())


class TestTransientSuppressionEndToEnd:
    async def test_fresh_last_synced_suppresses_write(
        self,
        seeded_account: dict[str, Any],
        db_engine: AsyncEngine,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Fresh last_synced_at (< window) -> last_sync_error NOT written;
        consecutive_failures untouched; account stays active; the WARNING records
        the suppression."""
        _force_window(monkeypatch, 60)
        account_id = seeded_account["account_id"]
        fresh = _dt.datetime.now(_dt.UTC) - _dt.timedelta(minutes=5)
        await _set_last_synced_at(db_engine, account_id, fresh)

        acc = await _reload(db_engine, account_id)
        cycle_log = sc.log.bind(mail_account_id=acc.id, user_id=acc.user_id)

        with structlog.testing.capture_logs() as logs:
            result = await sc._handle_sync_error(
                acc,
                _FLAKE,
                detail="User is authenticated but not connected",
                cycle_log=cycle_log,
            )

        assert result.outcome == "transient"
        events = {e["event"]: e for e in logs}
        assert "sync_account_transient" in events
        assert events["sync_account_transient"]["prefix"] == "network"
        assert events["sync_account_transient"]["last_sync_error_suppressed"] is True

        fresh_row = await _reload(db_engine, account_id)
        # SUPPRESSED: the sporadic flake never reaches the UI column.
        assert fresh_row.last_sync_error is None
        assert fresh_row.consecutive_failures == 0  # transient never bumps
        assert fresh_row.is_active is True

    async def test_stale_last_synced_writes_error(
        self,
        seeded_account: dict[str, Any],
        db_engine: AsyncEngine,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Stale last_synced_at (> window) -> the transient error IS written
        (the sync is genuinely stuck) but still never bumps the counter."""
        _force_window(monkeypatch, 60)
        account_id = seeded_account["account_id"]
        stale = _dt.datetime.now(_dt.UTC) - _dt.timedelta(hours=10)
        await _set_last_synced_at(db_engine, account_id, stale)

        acc = await _reload(db_engine, account_id)
        cycle_log = sc.log.bind(mail_account_id=acc.id, user_id=acc.user_id)

        with structlog.testing.capture_logs() as logs:
            result = await sc._handle_sync_error(
                acc,
                _FLAKE,
                detail="User is authenticated but not connected",
                cycle_log=cycle_log,
            )

        assert result.outcome == "transient"
        events = {e["event"]: e for e in logs}
        assert events["sync_account_transient"]["last_sync_error_suppressed"] is False

        fresh_row = await _reload(db_engine, account_id)
        assert fresh_row.last_sync_error is not None
        assert fresh_row.last_sync_error.startswith("network:")
        assert fresh_row.consecutive_failures == 0
        assert fresh_row.is_active is True

    async def test_null_last_synced_writes_error(
        self,
        seeded_account: dict[str, Any],
        db_engine: AsyncEngine,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Never-synced (NULL last_synced_at) -> the transient error IS written."""
        _force_window(monkeypatch, 60)
        account_id = seeded_account["account_id"]
        acc = await _reload(db_engine, account_id)
        assert acc.last_synced_at is None  # fresh seed never synced
        cycle_log = sc.log.bind(mail_account_id=acc.id, user_id=acc.user_id)

        result = await sc._handle_sync_error(
            acc,
            _FLAKE,
            detail="User is authenticated but not connected",
            cycle_log=cycle_log,
        )

        assert result.outcome == "transient"
        fresh_row = await _reload(db_engine, account_id)
        assert fresh_row.last_sync_error is not None
        assert fresh_row.last_sync_error.startswith("network:")
        assert fresh_row.consecutive_failures == 0
        assert fresh_row.is_active is True

    async def test_zero_window_always_writes(
        self,
        seeded_account: dict[str, Any],
        db_engine: AsyncEngine,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Window == 0 disables suppression even with a fresh last_synced_at."""
        _force_window(monkeypatch, 0)
        account_id = seeded_account["account_id"]
        fresh = _dt.datetime.now(_dt.UTC) - _dt.timedelta(minutes=1)
        await _set_last_synced_at(db_engine, account_id, fresh)

        acc = await _reload(db_engine, account_id)
        cycle_log = sc.log.bind(mail_account_id=acc.id, user_id=acc.user_id)

        result = await sc._handle_sync_error(acc, _FLAKE, detail="flake", cycle_log=cycle_log)

        assert result.outcome == "transient"
        fresh_row = await _reload(db_engine, account_id)
        assert fresh_row.last_sync_error is not None
        assert fresh_row.consecutive_failures == 0
