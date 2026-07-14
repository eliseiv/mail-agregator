"""TD-054 — runtime detector for a forgotten ``flush_crm_status_events()``.

Source of truth: ``docs/adr/ADR-0046-mailbox-status-hook-points.md`` §2.1 (the
domain service does NOT push — it collects ids and the CALLER flushes strictly
AFTER its COMMIT), §2.1.1 (the closed list of status-writing methods: the
``creds_changed`` branch of ``MailAccountService.update`` and
``MailAccountService.set_active``), §5 п.6 (the detector itself), and the
``TD-054`` row in ``docs/100-known-tech-debt.md``. Env key ``SESSION_GUARD_STRICT``
is specified in ``docs/07-deployment.md`` §4 "Session guards".

What is under test (``shared/session_guards.py``, registered in
``backend/app/accounts/service.py:149-157``):

- a caller that COMMITs a status-writing change and never flushes loses the
  event **silently** — for a deactivation irrecoverably (the box leaves
  ``list_active()``). The guard makes that loss noisy at session teardown
  (``shared/db.py:110`` ``get_session`` / ``:127`` ``make_session`` — the only two
  session sources), WITHOUT repairing it (no auto-flush: ADR-0046 §Alternatives);
- strict mode (``AssertionError``) is on automatically under pytest
  (``PYTEST_CURRENT_TEST``); production (``SESSION_GUARD_STRICT=0``) only logs the
  ``crm_status_pending_dropped`` warning and the already-committed request still
  returns 200;
- no false positives on the happy path (router flushes) nor on a rollback;
- no false NEGATIVE: a COMMIT followed by a LATER rollback (or a savepoint
  release) must NOT drain the ids whose status is already written to the DB.

The ``db_session`` fixture of ``tests/conftest.py:187`` builds its session with a
local ``async_sessionmaker`` — it bypasses ``shared/db.py`` entirely, so the
detector never runs there. Every case below therefore drives a REAL session
source: the ASGI stack (``get_session``) or ``make_session``.

This module lives under ``tests/worker`` because that package carries the
DB/Redis/MinIO cleanup fixtures and is inside the CI test scope
(``.github/workflows/ci.yml``: ``tests/unit tests/worker tests/frontend``).
"""

from __future__ import annotations

import gc
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from backend.app.accounts.schemas import MailAccountUpdateRequest
from backend.app.accounts.service import (
    CRM_STATUS_PENDING_DROPPED_EVENT,
    MailAccountService,
)
from backend.app.crm_push.service import CRM_STATUS_QUEUE_KEY, parse_status_payload
from backend.app.deps import VisibilityScope
from backend.app.repositories.mail_accounts import MailAccountsRepo
from shared import session_guards
from shared.config import get_settings
from shared.crypto import encrypt_mail_password
from shared.db import make_session
from shared.models import MailAccount, User
from shared.redis_client import get_redis

pytestmark = pytest.mark.integration  # needs DB + Redis + MinIO (app lifespan)

_API_KEY = "test-external-write-key"


class _Boom(Exception):
    """Domain-ish failure used to roll back a transaction from inside."""


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _structlog_isolation() -> Iterator[None]:
    """Do not leak the app's global structlog configuration out of this module.

    Booting the FastAPI app runs ``configure_logging()`` with
    ``cache_logger_on_first_use=True`` (``shared/logging.py``), which is
    process-global: suites that bind their logger before entering
    ``structlog.testing.capture_logs`` would then capture nothing. Snapshot and
    restore, so the state this module inherits is the state it hands on.
    """
    saved = structlog.get_config()
    yield
    structlog.configure(**saved)


@pytest.fixture(autouse=True)
def guard_warnings(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Capture the detector's structured warnings.

    ``shared.session_guards`` calls ``log.warning`` through its module global, so
    swapping the global is enough — and it survives the app's global structlog
    (re)configuration, unlike ``capture_logs``.
    """
    seen: list[dict[str, Any]] = []

    class _Recorder:
        def warning(self, event: str, **kw: Any) -> None:
            seen.append({"event": event, **kw})

    monkeypatch.setattr(session_guards, "log", _Recorder())
    return seen


@pytest.fixture
def crm_status_on(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Status channel ON + the external write API enabled (read at request time)."""
    monkeypatch.setenv("CRM_MAILBOX_STATUS_URL", "https://crm.example")
    monkeypatch.setenv("CRM_PUSH_SECRET", "test-status-secret")
    monkeypatch.setenv("EXTERNAL_API_KEY", _API_KEY)
    monkeypatch.setenv("EXTERNAL_WRITE_ENABLED", "true")
    get_settings.cache_clear()
    assert get_settings().crm_status_enabled is True
    yield
    get_settings.cache_clear()


@pytest.fixture
def no_imap_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the IMAP/SMTP connectivity probe (external boundary)."""

    async def _fake_test(self: Any, payload: Any, *, scope: Any = None) -> Any:
        # Imported lazily: the DTO's name would make pytest try to COLLECT it.
        from backend.app.accounts.schemas import TestResult

        return TestResult(imap_ok=True, smtp_ok=True)

    monkeypatch.setattr(MailAccountService, "test", _fake_test)


@pytest.fixture
def forgetful_caller(monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulate a caller that never flushes (the failure mode TD-054 detects)."""

    async def _no_flush(self: MailAccountService) -> None:
        return None

    monkeypatch.setattr(MailAccountService, "flush_crm_status_events", _no_flush)


def _factory(db_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(bind=db_engine, expire_on_commit=False, class_=AsyncSession)


async def _seed(
    db_engine: AsyncEngine,
    *,
    suffix: str,
    count: int = 1,
    **overrides: Any,
) -> tuple[int, list[int]]:
    """Seed one super-admin owner + ``count`` active password mailboxes."""
    account_ids: list[int] = []
    async with _factory(db_engine)() as ses, ses.begin():
        owner = User(
            username=f"td054_{suffix}",
            role="super_admin",
            password_hash="$argon2id$v=19$m=65536,t=3,p=4$abc$def",
            password_reset_required=False,
        )
        ses.add(owner)
        await ses.flush()
        for _ in range(count):
            new_id = await MailAccountsRepo(ses).next_account_id()
            ses.add(
                MailAccount(
                    id=new_id,
                    user_id=owner.id,
                    email=f"td054box{new_id}@example.com",
                    encrypted_password=encrypt_mail_password("p", new_id),
                    imap_host="imap.example.com",
                    imap_port=993,
                    imap_ssl=True,
                    smtp_host="smtp.example.com",
                    smtp_port=465,
                    smtp_ssl=True,
                    smtp_starttls=False,
                    **overrides,
                )
            )
            await ses.flush()
            account_ids.append(int(new_id))
        return int(owner.id), account_ids


def _scope(owner_id: int) -> VisibilityScope:
    return VisibilityScope(
        user_id=owner_id,
        role="super_admin",
        group_id=None,
        group_ids=frozenset(),
    )


async def _status_events() -> list[int]:
    raw = await get_redis().lrange(CRM_STATUS_QUEUE_KEY, 0, -1)
    out: list[int] = []
    for item in raw:
        decoded = item.decode() if isinstance(item, bytes) else item
        acc_id = parse_status_payload(decoded)
        assert acc_id is not None
        out.append(acc_id)
    return out


async def _load(db_engine: AsyncEngine, account_id: int) -> MailAccount | None:
    async with _factory(db_engine)() as ses:
        return await ses.get(MailAccount, account_id)


def _key_headers() -> dict[str, str]:
    return {"X-API-Key": _API_KEY, "Content-Type": "application/json"}


def _expected_warning(account_ids: list[int]) -> dict[str, Any]:
    return {
        "event": CRM_STATUS_PENDING_DROPPED_EVENT,
        "owner": "MailAccountService",
        "mail_account_ids": account_ids,
    }


# ---------------------------------------------------------------------------
# 1-2 — the detector fires on a forgotten flush (both methods of ADR-0046 §2.1.1)
# ---------------------------------------------------------------------------


class TestForgottenFlushIsDetected:
    async def test_set_active_committed_without_flush_raises_at_teardown(
        self,
        db_engine: AsyncEngine,
        crm_status_on: None,
        guard_warnings: list[dict[str, Any]],
    ) -> None:
        """H6 ``set_active`` + COMMIT + no flush → ``AssertionError`` naming the box.

        The deactivation is the irrecoverable case (ADR-0046 §2.1.1): the box drops
        out of ``list_active()`` and no second status event will ever be produced.
        """
        owner_id, (account_id,) = await _seed(db_engine, suffix="c1")

        with pytest.raises(AssertionError) as exc:
            async with make_session() as db:
                service = MailAccountService(db)
                async with db.begin():
                    await service.set_active(
                        scope=_scope(owner_id), account_id=account_id, is_active=False
                    )
                # The caller forgets ``await service.flush_crm_status_events()``.

        assert CRM_STATUS_PENDING_DROPPED_EVENT in str(exc.value)
        assert f"mail_account_ids=[{account_id}]" in str(exc.value)
        assert guard_warnings == [_expected_warning([account_id])]
        # The detector reports; it never repairs (no auto-flush — §Alternatives).
        assert await _status_events() == []
        # …and the write itself did commit — that is exactly why the loss matters.
        acc = await _load(db_engine, account_id)
        assert acc is not None
        assert acc.is_active is False

    async def test_update_creds_changed_committed_without_flush_raises_at_teardown(
        self,
        db_engine: AsyncEngine,
        crm_status_on: None,
        no_imap_probe: None,
        guard_warnings: list[dict[str, Any]],
    ) -> None:
        """H5 — the ``creds_changed`` branch of ``update``, the second (and last)
        status-writing method of the closed list in ADR-0046 §2.1.1."""
        owner_id, (account_id,) = await _seed(
            db_engine,
            suffix="c2",
            is_active=False,
            consecutive_failures=3,
            last_sync_error="auth_failed: bad password",
        )

        with pytest.raises(AssertionError) as exc:
            async with make_session() as db:
                service = MailAccountService(db)
                async with db.begin():
                    await service.update(
                        scope=_scope(owner_id),
                        account_id=account_id,
                        payload=MailAccountUpdateRequest(password="new-app-password"),
                    )
                # No flush → the re-enable is never mirrored to the CRM.

        assert f"mail_account_ids=[{account_id}]" in str(exc.value)
        assert guard_warnings == [_expected_warning([account_id])]
        assert await _status_events() == []
        acc = await _load(db_engine, account_id)
        assert acc is not None
        assert acc.is_active is True  # committed re-enable, un-mirrored


# ---------------------------------------------------------------------------
# 3-4 — no false positives (happy path through the routers; rollback path)
# ---------------------------------------------------------------------------


class TestNoFalsePositives:
    async def test_h5_patch_mail_account_with_router_flush_is_clean(
        self,
        db_engine: AsyncEngine,
        crm_status_on: None,
        no_imap_probe: None,
        client: httpx.AsyncClient,
        guard_warnings: list[dict[str, Any]],
    ) -> None:
        """Credential PATCH → H5 re-enable, with the router flushing after COMMIT.

        ADR-0044 §5: the session route ``PATCH /api/mail-accounts/{id}`` is gone with
        the cookie UI; the same ``MailAccountService.update`` call-site (and the same
        ``flush_crm_status_events`` in the router, ``external/router.py``) is reached
        through the surviving machine route.
        """
        _, (account_id,) = await _seed(
            db_engine,
            suffix="c3a",
            is_active=False,
            consecutive_failures=3,
            last_sync_error="auth_failed: bad password",
        )

        resp = await client.patch(
            f"/api/external/mailboxes/{account_id}",
            json={"password": "new-app-password"},
            headers=_key_headers(),
        )

        assert resp.status_code == 200, resp.text
        assert await _status_events() == [account_id]  # exactly one event
        assert guard_warnings == []  # the detector stayed silent

    async def test_h6_patch_external_mailbox_with_router_flush_is_clean(
        self,
        db_engine: AsyncEngine,
        crm_status_on: None,
        client: httpx.AsyncClient,
        guard_warnings: list[dict[str, Any]],
    ) -> None:
        """``PATCH /api/external/mailboxes/{id}`` (flush at ``external/router.py:511``)."""
        _, (account_id,) = await _seed(db_engine, suffix="c3b")

        resp = await client.patch(
            f"/api/external/mailboxes/{account_id}",
            json={"is_active": False},
            headers=_key_headers(),
        )

        assert resp.status_code == 200, resp.text
        assert await _status_events() == [account_id]
        assert guard_warnings == []

    async def test_rollback_inside_router_transaction_is_clean(
        self,
        db_engine: AsyncEngine,
        crm_status_on: None,
        no_imap_probe: None,
        client: httpx.AsyncClient,
        guard_warnings: list[dict[str, Any]],
    ) -> None:
        """409 raised inside ``db.begin()`` → rollback → nothing was written, so
        nothing was "dropped": neither a warning nor an ``AssertionError`` (which,
        in strict mode, would have surfaced as a 500 instead of the 409)."""
        _, (account_id,) = await _seed(
            db_engine,
            suffix="c4",
            is_active=False,
            consecutive_failures=3,
            last_sync_error="auth_failed: bad password",
        )

        resp = await client.patch(
            f"/api/external/mailboxes/{account_id}",
            json={"password": "new-app-password", "smtp_ssl": True, "smtp_starttls": True},
            headers=_key_headers(),
        )

        assert resp.status_code == 409, resp.text
        assert guard_warnings == []
        assert await _status_events() == []
        acc = await _load(db_engine, account_id)
        assert acc is not None
        assert acc.is_active is False  # nothing committed


# ---------------------------------------------------------------------------
# 5-7 — no false NEGATIVE: a later rollback must not silence a COMMITted id
# ---------------------------------------------------------------------------


class TestCommittedIdsSurviveALaterRollback:
    async def test_rollback_after_commit_still_reports_the_committed_id(
        self,
        db_engine: AsyncEngine,
        crm_status_on: None,
        guard_warnings: list[dict[str, Any]],
    ) -> None:
        """COMMIT → rollback in the NEXT (autobegin) transaction → the detector must
        still fire: the status is already in the DB, so the event is genuinely lost.

        Draining the queue on *any* rollback would silence the detector exactly
        where the loss is real (``_status_committed_count``,
        ``accounts/service.py:191``).
        """
        owner_id, (account_id,) = await _seed(db_engine, suffix="c5")

        with pytest.raises(AssertionError) as exc:
            async with make_session() as db:
                service = MailAccountService(db)
                async with db.begin():
                    await service.set_active(
                        scope=_scope(owner_id), account_id=account_id, is_active=False
                    )
                # COMMIT done. A later read autobegins a fresh transaction which the
                # caller then rolls back (an error handler, a failed SELECT, …).
                await db.execute(text("SELECT 1"))
                await db.rollback()

        assert f"mail_account_ids=[{account_id}]" in str(exc.value)
        assert guard_warnings == [_expected_warning([account_id])]
        acc = await _load(db_engine, account_id)
        assert acc is not None
        assert acc.is_active is False  # the rollback did NOT undo the COMMIT

    async def test_savepoint_release_does_not_mark_the_queue_committed(
        self,
        db_engine: AsyncEngine,
        crm_status_on: None,
        guard_warnings: list[dict[str, Any]],
    ) -> None:
        """Append in the OUTER txn → ``begin_nested()`` + release → outer ROLLBACK →
        the pending queue drains completely.

        SQLAlchemy dispatches ``after_commit`` for a SAVEPOINT release too; treating
        that as a real COMMIT would freeze the id and make the detector cry wolf on
        a fully rolled-back unit of work (``get_nested_transaction()`` discriminates —
        ``accounts/service.py:189``).
        """
        owner_id, (account_id,) = await _seed(db_engine, suffix="c6a")

        async with make_session() as db:
            service = MailAccountService(db)
            with pytest.raises(_Boom):
                async with db.begin():
                    await service.set_active(
                        scope=_scope(owner_id), account_id=account_id, is_active=False
                    )
                    async with db.begin_nested():  # savepoint …
                        await db.execute(text("SELECT 1"))
                    # … released here (after_commit fires, nested) — not a COMMIT.
                    raise _Boom
            assert service._pending_status_account_ids == []

        assert guard_warnings == []  # no false positive at teardown
        assert await _status_events() == []
        acc = await _load(db_engine, account_id)
        assert acc is not None
        assert acc.is_active is True  # outer rollback undid the write

    async def test_savepoint_rollback_leaves_the_pending_queue_untouched(
        self,
        db_engine: AsyncEngine,
        crm_status_on: None,
        guard_warnings: list[dict[str, Any]],
    ) -> None:
        """A rolled-back ``begin_nested()`` does not end the unit of work: the outer
        transaction may still COMMIT, so the queue must NOT be drained
        (``previous_transaction.nested`` → return, ``accounts/service.py:197``)."""
        owner_id, (account_id,) = await _seed(db_engine, suffix="c6b")

        async with make_session() as db:
            service = MailAccountService(db)
            async with db.begin():
                with pytest.raises(_Boom):
                    async with db.begin_nested():
                        await service.set_active(
                            scope=_scope(owner_id), account_id=account_id, is_active=False
                        )
                        raise _Boom
                # Savepoint rollback: the id stays pending, the caller still owns it.
                assert service._pending_status_account_ids == [account_id]
            await service.flush_crm_status_events()

        assert guard_warnings == []
        assert await _status_events() == [account_id]

    async def test_mixed_commit_then_rolled_back_transaction_reports_only_the_committed_id(
        self,
        db_engine: AsyncEngine,
        crm_status_on: None,
        guard_warnings: list[dict[str, Any]],
    ) -> None:
        """COMMIT id=A, then append id=B in a second transaction that rolls back →
        the detector must report EXACTLY ``[A]``."""
        owner_id, (a_id, b_id) = await _seed(db_engine, suffix="c7", count=2)
        scope = _scope(owner_id)

        with pytest.raises(AssertionError) as exc:
            async with make_session() as db:
                service = MailAccountService(db)
                async with db.begin():
                    await service.set_active(scope=scope, account_id=a_id, is_active=False)
                with pytest.raises(_Boom):
                    async with db.begin():
                        await service.set_active(scope=scope, account_id=b_id, is_active=False)
                        raise _Boom

        # The rendered payload is the whole list — ``[A]``, not ``[A, B]``.
        assert f"mail_account_ids=[{a_id}]" in str(exc.value)
        assert guard_warnings == [_expected_warning([a_id])]
        acc_b = await _load(db_engine, b_id)
        assert acc_b is not None
        assert acc_b.is_active is True  # B's change never landed → nothing to mirror


# ---------------------------------------------------------------------------
# 8 — production mode: warn, never fail the already-committed request
# ---------------------------------------------------------------------------


class TestProductionMode:
    async def test_forgotten_flush_warns_once_and_the_patch_still_returns_200(
        self,
        db_engine: AsyncEngine,
        crm_status_on: None,
        forgetful_caller: None,
        client: httpx.AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
        guard_warnings: list[dict[str, Any]],
    ) -> None:
        """``SESSION_GUARD_STRICT=0`` (prod): exactly one ``crm_status_pending_dropped``
        warning carrying ``mail_account_ids``, and the committed PATCH keeps its 200
        (ADR-0046 §5 п.6 / ``07-deployment.md`` §4 "Session guards")."""
        monkeypatch.setenv(session_guards.STRICT_ENV_VAR, "0")
        assert session_guards.strict_mode() is False
        _, (account_id,) = await _seed(db_engine, suffix="c8")

        resp = await client.patch(
            f"/api/external/mailboxes/{account_id}",
            json={"is_active": False},
            headers=_key_headers(),
        )

        assert resp.status_code == 200, resp.text
        assert resp.json()["is_active"] is False
        assert guard_warnings == [_expected_warning([account_id])]
        assert await _status_events() == []  # the event is lost — that is the point
        acc = await _load(db_engine, account_id)
        assert acc is not None
        assert acc.is_active is False


# ---------------------------------------------------------------------------
# 9 — the registry does not leak sessions that never reach the shared.db teardown
# ---------------------------------------------------------------------------


class TestRegistryDoesNotLeak:
    async def test_session_created_outside_shared_db_is_garbage_collected(
        self, db_engine: AsyncEngine
    ) -> None:
        """A session built directly (a ``db_session``-style fixture, a future caller)
        never passes through ``check_session_guards`` — it must still be collectable.

        The registry is a ``WeakKeyDictionary`` keyed by the session, so the guard's
        probe must not strongly reference the session (nor the service holding it) —
        see the warning in ``SessionGuard``'s docstring.
        """
        gc.collect()
        assert len(session_guards._registry) == 0

        session = AsyncSession(bind=db_engine, expire_on_commit=False)
        service = MailAccountService(session)
        assert len(session_guards._registry) == 1

        del service
        del session
        gc.collect()

        assert len(session_guards._registry) == 0


# ---------------------------------------------------------------------------
# 10-11 — worker path without pending work; repeated flush
# ---------------------------------------------------------------------------


class TestWorkerPathAndIdempotentFlush:
    async def test_make_session_without_pending_status_is_silent(
        self,
        db_engine: AsyncEngine,
        crm_status_on: None,
        guard_warnings: list[dict[str, Any]],
    ) -> None:
        """The worker/CLI session source (``shared/db.py``): a session that only
        READS never enqueues anything → the detector must not fire.

        ADR-0044 §5: ``MailAccountService.get_for_scope`` served the HTML detail page
        and went away with it; the read below goes through the surviving repository
        (the same read the worker itself does) inside the same ``make_session()``
        source the detector watches.
        """
        owner_id, (account_id,) = await _seed(db_engine, suffix="c10")

        async with make_session() as db:
            service = MailAccountService(db)  # instantiating must not enqueue anything
            assert await service.visible_user_ids(_scope(owner_id)) is None
            acc = await MailAccountsRepo(db).get_by_id(account_id)
            assert acc is not None and acc.id == account_id

        assert guard_warnings == []
        assert await _status_events() == []

    async def test_second_flush_is_a_no_op(
        self,
        db_engine: AsyncEngine,
        crm_status_on: None,
        guard_warnings: list[dict[str, Any]],
    ) -> None:
        """The queue is drained in place → a repeated flush enqueues nothing more."""
        owner_id, (account_id,) = await _seed(db_engine, suffix="c11")

        async with make_session() as db:
            service = MailAccountService(db)
            async with db.begin():
                await service.set_active(
                    scope=_scope(owner_id), account_id=account_id, is_active=False
                )
            await service.flush_crm_status_events()
            await service.flush_crm_status_events()  # no-op

        assert await _status_events() == [account_id]  # exactly one event
        assert guard_warnings == []
