"""ADR-0026 observability + phase-2 timestamp semantics (live PG).

Supplements:

* Scope A (MINOR-2): a rule-10 fail-open programming error (``TypeError``) routed
  through ``sync_cycle._handle_sync_error`` MUST classify TRANSIENT, write
  ``last_sync_error`` (prefix ``error:``), and emit an ERROR-level
  ``sync_account_unexpected_error`` event WITH ``exc_info`` (traceback) for
  alerting — not a silent WARNING. A recognised transient (``timeout``) must
  instead emit the WARNING ``sync_account_transient`` event (no ERROR).
* Scope C: the PERMANENT phase-2 write (``mark_sync_failure``) sets
  ``last_synced_at = now()`` (its documented semantics), whereas a TRANSIENT
  write (``mark_transient_error``) leaves ``last_synced_at`` untouched. This pins
  the asymmetry ADR-0026 §2 relies on.

These need a real Postgres (docker-compose.test.yml). They reuse the worker
package's autouse DB/Redis/MinIO truncation fixtures.
"""

from __future__ import annotations

from typing import Any

import pytest
import structlog
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from shared.crypto import encrypt_mail_password
from shared.models import MailAccount, User
from worker.app import sync_cycle as sc

pytestmark = pytest.mark.integration  # needs the DB to be live


@pytest.fixture
async def seeded_account(db_engine: AsyncEngine) -> dict[str, Any]:
    """Seed a super-admin + one active mail_account; return ids + the ORM row."""
    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with factory() as ses, ses.begin():
        admin = User(
            username="obs_admin",
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
            email="obs@example.com",
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


class TestRule10ErrorLogging:
    async def test_unexpected_error_logs_error_with_traceback(
        self, seeded_account: dict[str, Any], db_engine: AsyncEngine
    ) -> None:
        """Rule 10: a TypeError (our own bug) -> transient outcome + last_sync_error
        with the ``error:`` prefix + an ERROR ``sync_account_unexpected_error``
        event carrying exc_info (traceback). Class stays transient so we never
        disable on our own bug, but the ERROR log is the alerting signal."""
        acc = await _reload(db_engine, seeded_account["account_id"])
        cycle_log = sc.log.bind(mail_account_id=acc.id, user_id=acc.user_id)

        with structlog.testing.capture_logs() as logs:
            result = await sc._handle_sync_error(
                acc,
                TypeError("argument of type 'int' is not iterable"),
                detail="argument of type 'int' is not iterable",
                cycle_log=cycle_log,
            )

        assert result.outcome == "transient"  # fail-open, never permanent
        events = {e["event"]: e for e in logs}
        assert "sync_account_unexpected_error" in events, events
        err = events["sync_account_unexpected_error"]
        assert err["log_level"] == "error"
        # exc_info=True is recorded by structlog's capture_logs as exc_info key.
        assert err.get("exc_info") is True
        # WARNING transient event must NOT be emitted for a rule-10 error.
        assert "sync_account_transient" not in events

        fresh = await _reload(db_engine, seeded_account["account_id"])
        assert fresh.last_sync_error is not None
        assert fresh.last_sync_error.startswith("error:")
        assert fresh.consecutive_failures == 0  # transient never bumps
        assert fresh.is_active is True

    async def test_recognised_transient_logs_warning_not_error(
        self, seeded_account: dict[str, Any], db_engine: AsyncEngine
    ) -> None:
        """A recognised transient (timeout) -> WARNING ``sync_account_transient``,
        never the ERROR ``sync_account_unexpected_error`` event."""
        acc = await _reload(db_engine, seeded_account["account_id"])
        cycle_log = sc.log.bind(mail_account_id=acc.id, user_id=acc.user_id)

        with structlog.testing.capture_logs() as logs:
            result = await sc._handle_sync_error(
                acc,
                TimeoutError("timed out"),
                detail="timed out",
                cycle_log=cycle_log,
            )

        assert result.outcome == "transient"
        events = {e["event"]: e for e in logs}
        assert "sync_account_transient" in events
        assert events["sync_account_transient"]["log_level"] == "warning"
        assert "sync_account_unexpected_error" not in events

        fresh = await _reload(db_engine, seeded_account["account_id"])
        assert fresh.last_sync_error is not None
        assert fresh.last_sync_error.startswith("timeout:")


class TestPhase2TimestampSemantics:
    async def test_permanent_failure_sets_last_synced_at(
        self, seeded_account: dict[str, Any], db_engine: AsyncEngine
    ) -> None:
        """Scope C: the PERMANENT phase-2 bump (mark_sync_failure) advances
        ``last_synced_at`` to now() (documented behaviour)."""
        from backend.app.repositories.mail_accounts import MailAccountsRepo

        account_id = seeded_account["account_id"]
        before = await _reload(db_engine, account_id)
        assert before.last_synced_at is None  # fresh account never synced

        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as ses, ses.begin():
            await MailAccountsRepo(ses).mark_sync_failure(
                account_id, error="auth_failed: bad", disable=False
            )

        fresh = await _reload(db_engine, account_id)
        assert fresh.last_synced_at is not None  # permanent advances it
        assert fresh.consecutive_failures == 1

    async def test_transient_does_not_set_last_synced_at(
        self, seeded_account: dict[str, Any], db_engine: AsyncEngine
    ) -> None:
        """Scope C complement: a TRANSIENT write never advances last_synced_at."""
        from backend.app.repositories.mail_accounts import MailAccountsRepo

        account_id = seeded_account["account_id"]
        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as ses, ses.begin():
            await MailAccountsRepo(ses).mark_transient_error(
                account_id, error="invalid_host: Could not resolve host"
            )

        fresh = await _reload(db_engine, account_id)
        assert fresh.last_synced_at is None  # untouched by transient
        assert fresh.last_sync_error == "invalid_host: Could not resolve host"
