"""Live-PG tests for ADR-0026 §2/§3: two-phase circuit-breaker + repo writes.

Scopes B, C, D. These require a real Postgres (docker-compose.test.yml). The DB
URL is taken from ``TEST_DATABASE_URL`` (falls back to ``DATABASE_URL``).

Key design:
* We pin ``DATABASE_URL`` to the test DB and force the worker's global engine +
  session factory (``shared.db``) to ONE engine built on each test's event loop.
  ``_run_for_accounts`` phase 2 uses ``make_session()`` internally, so this makes
  the worker writes and our seeds/reads share one loop + connection pool
  (pytest-asyncio gives each test a fresh loop; asyncpg connections can't cross
  loops).
* ``sync_one_account`` is mocked at the seam (no IMAP / no network): each test
  declares per-account outcomes; phase 2 (bump / disable / audit) runs for REAL
  against PG so we assert true row state.
* Each test seeds rows under unique names and deletes the accounts/users/audit it
  created at teardown, so tests stay isolated and order-independent.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

pytestmark = pytest.mark.asyncio


def _test_db_url() -> str:
    return os.environ.get(
        "TEST_DATABASE_URL",
        os.environ.get(
            "DATABASE_URL",
            "postgresql+asyncpg://mas:changeme_in_prod_postgres@127.0.0.1:55432/mail_aggregator",
        ),
    )


# Pin DATABASE_URL so the worker's make_session() binds to the test DB.
os.environ["DATABASE_URL"] = _test_db_url()

# NOTE: ``shared.models`` exports ``AdminAudit`` (table ``admin_audit``) — there is
# no ``AuditLog``; the declarative base lives in ``shared.db``.
from shared.models import AdminAudit, MailAccount, User
from worker.app import sync_cycle as sc
from worker.app.sync_cycle import _AccountResult

# Local alias so the assertions below read naturally against the audit table.
AuditLog = AdminAudit


@pytest_asyncio.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    """One engine per test, ALSO installed as the worker's global engine.

    pytest-asyncio runs each test on a fresh event loop; the worker's global
    asyncpg engine pins itself to whichever loop first awaited it. We dispose any
    prior global engine and install THIS test's engine as the global one, so
    ``_run_for_accounts`` -> ``make_session()`` writes on the same loop + pool we
    then read from. Schema is already provisioned via ``alembic upgrade head``.
    """
    from shared.config import get_settings

    os.environ["DATABASE_URL"] = _test_db_url()
    get_settings.cache_clear()

    from shared import db as _shared_db

    await _shared_db.dispose_engine()

    # Generous pool: the mass-failure test runs up to MAX_CONCURRENT_IMAP worker
    # ``make_session()`` connections CONCURRENTLY while the test ``session``
    # fixture also holds one. The default pool (5+5) could deadlock at the
    # boundary; 20+20 leaves ample headroom so no acquire ever blocks.
    eng = create_async_engine(
        _test_db_url(),
        echo=False,
        future=True,
        pool_pre_ping=True,
        pool_size=20,
        max_overflow=20,
    )
    _shared_db._engine = eng
    _shared_db._session_factory = async_sessionmaker(
        bind=eng, class_=AsyncSession, expire_on_commit=False
    )

    yield eng

    await _shared_db.dispose_engine()


@pytest_asyncio.fixture
async def session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as s:
        yield s


@pytest_asyncio.fixture
async def cleanup(engine: AsyncEngine) -> AsyncIterator[list[int]]:
    """Track created user ids; delete their accounts + audit rows at teardown."""
    created_user_ids: list[int] = []
    yield created_user_ids
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as s, s.begin():
        if created_user_ids:
            await s.execute(delete(MailAccount).where(MailAccount.user_id.in_(created_user_ids)))
            await s.execute(delete(AuditLog).where(AuditLog.target_user_id.in_(created_user_ids)))
            await s.execute(delete(User).where(User.id.in_(created_user_ids)))


async def _seed_user(
    session: AsyncSession, cleanup: list[int], *, role: str = "super_admin"
) -> User:
    """Seed a user. Default ``super_admin`` because:

    * ``ck_users_role`` only allows super_admin / group_leader / group_member;
    * ``users_role_group_invariant`` requires a NON-NULL group_id for the two
      group roles (we don't seed groups here);
    * the audit writer resolves the actor via ``UsersRepo.get_admin()``
      (role=super_admin), so disable/suppress audits attribute correctly.
    """
    suffix = uuid.uuid4().hex[:10]
    u = User(
        username=f"u-{suffix}",
        email=f"u-{suffix}@example.com",
        password_hash="x",
        role=role,
        password_reset_required=False,
    )
    session.add(u)
    await session.flush()
    cleanup.append(u.id)
    return u


async def _seed_account(
    session: AsyncSession, user: User, *, consecutive_failures: int = 0
) -> MailAccount:
    acc = MailAccount(
        user_id=user.id,
        email=f"box-{uuid.uuid4().hex[:10]}@example.com",
        encrypted_password=b"\x00" * 32,
        imap_host="imap.example.com",
        imap_port=993,
        imap_ssl=True,
        smtp_host="smtp.example.com",
        smtp_port=587,
        smtp_ssl=False,
        smtp_starttls=True,
        is_active=True,
        consecutive_failures=consecutive_failures,
    )
    session.add(acc)
    await session.flush()
    return acc


def _patch_outcomes(monkeypatch: pytest.MonkeyPatch, outcomes: dict[int, _AccountResult]) -> None:
    """Patch ``sync_one_account`` to return a pre-declared result per account id.

    Mirrors production phase 0: a transient/permanent outcome writes
    ``last_sync_error`` via the no-bump repo method, so breaker tests can assert
    "last_sync_error written even when the disable is suppressed".
    """

    async def _fake(account: MailAccount, **_kw: object) -> _AccountResult:
        res = outcomes[account.id]
        if res.outcome in ("transient", "permanent"):
            await sc._record_transient(account.id, error=res.error or f"{res.prefix}: x")
        return res

    monkeypatch.setattr(sc, "sync_one_account", _fake)


async def _reload(session: AsyncSession, account_id: int) -> MailAccount:
    """Read an account on a fresh session from the WORKER's global engine.

    Read-back correctness is the subtle part: ``_run_for_accounts`` commits on
    the worker's ``make_session()`` connections; the long-lived test ``session``
    holds an older snapshot. We open a NEW ``make_session()`` (same global engine
    the worker just wrote on, already pinned to this test's loop by the ``engine``
    fixture), which BEGINs a fresh transaction AFTER those commits and therefore
    observes them. Reusing the global engine (vs. spinning a throwaway engine per
    read) keeps connection churn bounded so the 85-account case doesn't exhaust
    the pool.
    """
    from shared.db import make_session

    async with make_session() as s:
        acc = await s.get(MailAccount, account_id)
    assert acc is not None
    return acc


async def _audit_rows(session: AsyncSession, action: str) -> list[AdminAudit]:
    # Fresh session on the worker's global engine (see _reload rationale).
    from shared.db import make_session

    async with make_session() as s:
        rows = (await s.execute(select(AuditLog).where(AuditLog.action == action))).scalars().all()
    return list(rows)


def _details(row: AdminAudit) -> dict[str, object]:
    """Typed accessor for the JSONB ``details`` payload (keeps mypy happy)."""
    payload = row.details
    assert payload is not None
    return payload


def _perm(
    account_id: int, user_id: int, *, explicit: bool, prefix: str = "auth_failed"
) -> _AccountResult:
    return _AccountResult(
        account_id=account_id,
        user_id=user_id,
        new_count=0,
        conflict_count=0,
        outcome="permanent",
        error=f"{prefix}: detail",
        prefix=prefix,
        explicit_permanent=explicit,
    )


def _ok(account_id: int, user_id: int) -> _AccountResult:
    return _AccountResult(
        account_id=account_id, user_id=user_id, new_count=0, conflict_count=0, outcome="ok"
    )


# ---------------------------------------------------------------------------
# D. Repo write semantics
# ---------------------------------------------------------------------------


class TestRepoWrites:
    async def test_mark_transient_error_does_not_touch_counters(
        self, session: AsyncSession, cleanup: list[int]
    ) -> None:
        from backend.app.repositories.mail_accounts import MailAccountsRepo

        u = await _seed_user(session, cleanup)
        acc = await _seed_account(session, u, consecutive_failures=2)
        await session.commit()

        async with session.begin():
            await MailAccountsRepo(session).mark_transient_error(acc.id, error="network: x")

        fresh = await _reload(session, acc.id)
        assert fresh.last_sync_error == "network: x"
        assert fresh.consecutive_failures == 2  # UNCHANGED
        assert fresh.is_active is True  # NOT disabled
        assert fresh.last_synced_at is None  # not touched (no successful sync yet)

    async def test_mark_sync_failure_bumps_counter(
        self, session: AsyncSession, cleanup: list[int]
    ) -> None:
        from backend.app.repositories.mail_accounts import MailAccountsRepo

        u = await _seed_user(session, cleanup)
        acc = await _seed_account(session, u)
        await session.commit()

        async with session.begin():
            new_count = await MailAccountsRepo(session).mark_sync_failure(
                acc.id, error="auth_failed: x", disable=False
            )
        assert new_count == 1
        fresh = await _reload(session, acc.id)
        assert fresh.consecutive_failures == 1
        assert fresh.is_active is True

    async def test_mark_sync_failure_with_disable(
        self, session: AsyncSession, cleanup: list[int]
    ) -> None:
        from backend.app.repositories.mail_accounts import MailAccountsRepo

        u = await _seed_user(session, cleanup)
        acc = await _seed_account(session, u)
        await session.commit()

        async with session.begin():
            await MailAccountsRepo(session).mark_sync_failure(
                acc.id, error="auth_failed: x", disable=True
            )
        fresh = await _reload(session, acc.id)
        assert fresh.is_active is False

    async def test_mark_sync_success_resets_recovery(
        self, session: AsyncSession, cleanup: list[int]
    ) -> None:
        from backend.app.repositories.mail_accounts import MailAccountsRepo

        u = await _seed_user(session, cleanup)
        acc = await _seed_account(session, u, consecutive_failures=2)
        await session.commit()
        async with session.begin():
            await MailAccountsRepo(session).mark_transient_error(acc.id, error="auth_failed: stale")

        async with session.begin():
            await MailAccountsRepo(session).mark_sync_success(
                acc.id, last_synced_uidnext=42, last_uidvalidity=7
            )
        fresh = await _reload(session, acc.id)
        assert fresh.last_sync_error is None
        assert fresh.consecutive_failures == 0
        assert fresh.last_synced_at is not None
        assert fresh.last_synced_uidnext == 42

    async def test_transient_then_success_no_double_write(
        self, session: AsyncSession, cleanup: list[int]
    ) -> None:
        """A transient record never bumps, so success has nothing to "undo"."""
        from backend.app.repositories.mail_accounts import MailAccountsRepo

        u = await _seed_user(session, cleanup)
        acc = await _seed_account(session, u)
        await session.commit()
        async with session.begin():
            await MailAccountsRepo(session).mark_transient_error(acc.id, error="timeout: x")
        mid = await _reload(session, acc.id)
        assert mid.consecutive_failures == 0
        async with session.begin():
            await MailAccountsRepo(session).mark_sync_success(
                acc.id, last_synced_uidnext=1, last_uidvalidity=1
            )
        fresh = await _reload(session, acc.id)
        assert fresh.consecutive_failures == 0
        assert fresh.last_sync_error is None


# ---------------------------------------------------------------------------
# B / C. Two-phase circuit-breaker via _run_for_accounts (live PG)
# ---------------------------------------------------------------------------


class TestCircuitBreaker:
    async def test_mass_permanent_failure_trips_breaker(
        self, session: AsyncSession, cleanup: list[int], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """B + C: 9/10 permanent (ratio 0.9 > 0.5, total 10 >= 5) -> breaker tripped.

        Models the production incident (81/85 -> mass disable) at a scale the
        single pytest-asyncio event loop handles deterministically. The
        per-account fan-out is gated by ``MAX_CONCURRENT_IMAP`` (default 10), so a
        10-account batch fills exactly one semaphore window with no queuing —
        keeping the ``asyncio.gather`` of real ``make_session()`` writes stable.
        (A literal 85-way gather of DB-writing coroutines deadlocks this single
        test loop — an asyncio/pytest harness limit, not a production issue; prod
        runs the same path under APScheduler's own loop.)

        Expect: 0 disabled, 0 bump, last_sync_error SET on every permanent
        (phase 0), is_active stays True, ONE suppression audit row carrying the
        real ratio/counts, NO per-account auto-disable audit."""
        n_total, n_perm = 10, 9
        u = await _seed_user(session, cleanup)
        accounts = [await _seed_account(session, u) for _ in range(n_total)]
        await session.commit()

        outcomes: dict[int, _AccountResult] = {}
        for i, acc in enumerate(accounts):
            outcomes[acc.id] = (
                _perm(acc.id, u.id, explicit=True) if i < n_perm else _ok(acc.id, u.id)
            )
        _patch_outcomes(monkeypatch, outcomes)

        await sc._run_for_accounts(accounts)

        disabled = bumped = with_err = 0
        for acc in accounts[:n_perm]:
            fresh = await _reload(session, acc.id)
            disabled += int(not fresh.is_active)
            bumped += int(fresh.consecutive_failures != 0)
            with_err += int(bool(fresh.last_sync_error))

        assert disabled == 0, "breaker must suppress ALL disables"
        assert bumped == 0, "breaker must suppress ALL counter bumps"
        assert with_err == n_perm, "phase-0 last_sync_error must be written for all permanents"

        rows = await _audit_rows(session, "sync_mass_failure_suppressed")
        assert len(rows) == 1
        details = _details(rows[0])
        assert details["permanent_failures"] == n_perm
        assert details["total"] == n_total
        assert await _audit_rows(session, "account_auto_disabled") == []

    async def test_single_permanent_below_ratio_disables_normally(
        self, session: AsyncSession, cleanup: list[int], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """1/85 permanent -> ratio ~0.012 < 0.5 -> breaker NOT tripped.

        The single explicit-permanent account is bumped AND disabled."""
        u = await _seed_user(session, cleanup)
        accounts = [await _seed_account(session, u) for _ in range(85)]
        await session.commit()

        bad = accounts[0]
        outcomes = {bad.id: _perm(bad.id, u.id, explicit=True)}
        for acc in accounts[1:]:
            outcomes[acc.id] = _ok(acc.id, u.id)
        _patch_outcomes(monkeypatch, outcomes)

        await sc._run_for_accounts(accounts)

        fresh = await _reload(session, bad.id)
        assert fresh.is_active is False  # disabled
        assert fresh.consecutive_failures == 1  # bumped
        assert fresh.last_sync_error is not None

        assert await _audit_rows(session, "sync_mass_failure_suppressed") == []
        assert len(await _audit_rows(session, "account_auto_disabled")) == 1

    async def test_force_sync_single_account_below_min_disables(
        self, session: AsyncSession, cleanup: list[int], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """1/1 (total < SYNC_MASS_FAILURE_MIN=5) -> breaker not considered.

        Mirrors force_sync on one account: it disables normally, never shielded
        by the mass-failure breaker."""
        u = await _seed_user(session, cleanup)
        acc = await _seed_account(session, u)
        await session.commit()

        _patch_outcomes(monkeypatch, {acc.id: _perm(acc.id, u.id, explicit=True)})

        await sc._run_for_accounts([acc])

        fresh = await _reload(session, acc.id)
        assert fresh.is_active is False
        assert fresh.consecutive_failures == 1
        assert await _audit_rows(session, "sync_mass_failure_suppressed") == []

    async def test_threshold_permanent_non_explicit_disables_after_n(
        self, session: AsyncSession, cleanup: list[int], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-explicit permanent disables only after
        SYNC_MAX_CONSECUTIVE_FAILURES (default 3).

        Pre-seed consecutive_failures=2; one more permanent bump (-> 3) disables.
        Single account: total<min so breaker NOT tripped -> normal path."""
        u = await _seed_user(session, cleanup)
        acc = await _seed_account(session, u, consecutive_failures=2)
        await session.commit()

        _patch_outcomes(monkeypatch, {acc.id: _perm(acc.id, u.id, explicit=False, prefix="error")})

        await sc._run_for_accounts([acc])

        fresh = await _reload(session, acc.id)
        assert fresh.consecutive_failures == 3
        assert fresh.is_active is False  # threshold reached

    async def test_non_explicit_below_threshold_does_not_disable(
        self, session: AsyncSession, cleanup: list[int], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-explicit permanent at count 1 (< threshold 3) bumps but does NOT
        disable."""
        u = await _seed_user(session, cleanup)
        acc = await _seed_account(session, u)
        await session.commit()

        _patch_outcomes(monkeypatch, {acc.id: _perm(acc.id, u.id, explicit=False, prefix="error")})

        await sc._run_for_accounts([acc])

        fresh = await _reload(session, acc.id)
        assert fresh.consecutive_failures == 1
        assert fresh.is_active is True

    async def test_phase0_breaker_tripped_preserves_active_and_counter(
        self, session: AsyncSession, cleanup: list[int], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """C: under a tripped breaker, a permanent account keeps is_active=True and
        consecutive_failures UNCHANGED while last_sync_error is set."""
        u = await _seed_user(session, cleanup)
        accounts = [await _seed_account(session, u, consecutive_failures=1) for _ in range(6)]
        await session.commit()

        _patch_outcomes(
            monkeypatch, {acc.id: _perm(acc.id, u.id, explicit=True) for acc in accounts}
        )  # 6/6 permanent -> ratio 1.0, total 6 >= 5

        await sc._run_for_accounts(accounts)

        for acc in accounts:
            fresh = await _reload(session, acc.id)
            assert fresh.is_active is True
            assert fresh.consecutive_failures == 1  # NOT bumped
            assert fresh.last_sync_error is not None

    async def test_transient_mass_does_not_trip_breaker_and_never_disables(
        self, session: AsyncSession, cleanup: list[int], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A mass TRANSIENT outage — breaker is keyed on PERMANENT ratio so it does
        not trip; transient never disables/bumps but records last_sync_error."""
        u = await _seed_user(session, cleanup)
        accounts = [await _seed_account(session, u) for _ in range(6)]
        await session.commit()

        outcomes = {
            acc.id: _AccountResult(
                account_id=acc.id,
                user_id=u.id,
                new_count=0,
                conflict_count=0,
                outcome="transient",
                error="invalid_host: Could not resolve host",
            )
            for acc in accounts
        }
        _patch_outcomes(monkeypatch, outcomes)

        await sc._run_for_accounts(accounts)

        for acc in accounts:
            fresh = await _reload(session, acc.id)
            assert fresh.is_active is True
            assert fresh.consecutive_failures == 0
            assert fresh.last_sync_error is not None
        assert await _audit_rows(session, "sync_mass_failure_suppressed") == []
        assert await _audit_rows(session, "account_auto_disabled") == []
