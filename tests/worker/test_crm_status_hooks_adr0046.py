"""ADR-0046 — mailbox status-channel: worker-side hook points + negative requirements.

Source of truth: ``docs/adr/ADR-0046-mailbox-status-hook-points.md``
(§1 ``last_synced_at`` semantics, §2 hook invariant "enqueue strictly AFTER
COMMIT", §3 H1-H4/H7a/H7b, §4 N3-N6, §5 "every point gets its own case").

Covered here (the backend-API points H5/H6 + N1/N2/N7 live in the sibling
module ``test_crm_status_hooks_api_adr0046.py``):

- **H1** ``mark_sync_success`` (successful cycle);
- **H2** ``_record_transient`` (TRANSIENT not suppressed **and** PERMANENT phase 0);
- **H3** ``_record_failure`` (phase 2 bump);
- **H4** ``_disable_after_failures`` (auto-disable);
- **H7a** transition into ``oauth_needs_consent`` (Microsoft ``invalid_grant``);
- **H7b** clean-skip branch of an already-needs-consent mailbox + **idempotency**;
- **N3** oauth-token writes, **N4** suppressed TRANSIENT, **N5** circuit-breaker,
  **N6** ``mark_sync_failure(disable=True)``;
- **§1** ``mark_sync_failure`` no longer stamps ``last_synced_at`` — and the
  regression that made ``_should_suppress_transient`` silently swallow the next
  TRANSIENT error;
- **§2** the enqueue happens AFTER the COMMIT: the hook spy reads the row through
  a *separate* session, so it can only observe the POST-state if the transaction
  is already committed;
- best-effort: a Redis outage inside the hook never breaks the sync cycle; with
  ``crm_status_enabled=false`` Redis is not touched at all.

The status channel is OFF by default in the test env (no ``CRM_MAILBOX_STATUS_URL``
/ ``CRM_PUSH_SECRET``), so every positive case explicitly enables it.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from backend.app.crm_push.service import CRM_STATUS_QUEUE_KEY, parse_status_payload
from backend.app.oauth.service import OAuthRefreshInvalidError, _TokenClient
from backend.app.repositories.mail_accounts import (
    OAUTH_NEEDS_CONSENT_SYNC_ERROR,
    MailAccountsRepo,
)
from shared.config import get_settings
from shared.crypto import encrypt_mail_password
from shared.models import MailAccount, User
from shared.redis_client import get_redis
from worker.app import sync_cycle as sc
from worker.app.imap_fetcher import FetchedBox

pytestmark = pytest.mark.integration  # needs DB + Redis


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def crm_status_on(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Enable the status channel (URL + secret) — ``crm_status_enabled`` is a
    derived property, so both env vars are required."""
    monkeypatch.setenv("CRM_MAILBOX_STATUS_URL", "https://crm.example")
    monkeypatch.setenv("CRM_PUSH_SECRET", "test-status-secret")
    get_settings.cache_clear()
    assert get_settings().crm_status_enabled is True
    yield
    get_settings.cache_clear()


@pytest.fixture
def crm_status_off(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv("CRM_MAILBOX_STATUS_URL", raising=False)
    monkeypatch.delenv("CRM_PUSH_SECRET", raising=False)
    get_settings.cache_clear()
    assert get_settings().crm_status_enabled is False
    yield
    get_settings.cache_clear()


def _factory(db_engine: AsyncEngine) -> async_sessionmaker[Any]:
    return async_sessionmaker(bind=db_engine, expire_on_commit=False)


async def _seed_account(db_engine: AsyncEngine, **overrides: Any) -> int:
    """Seed a user + one password mailbox. Returns ``(account_id)``."""
    async with _factory(db_engine)() as ses, ses.begin():
        owner = User(
            username=f"hooks_{overrides.pop('username_suffix', 'owner')}",
            role="super_admin",
            password_hash="$argon2id$v=19$m=65536,t=3,p=4$abc$def",
            password_reset_required=False,
        )
        ses.add(owner)
        await ses.flush()
        new_id = await MailAccountsRepo(ses).next_account_id()
        acc = MailAccount(
            id=new_id,
            user_id=owner.id,
            email=f"hooks{new_id}@example.com",
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
        ses.add(acc)
        await ses.flush()
        return int(acc.id)


async def _seed_oauth_account(db_engine: AsyncEngine, **overrides: Any) -> int:
    async with _factory(db_engine)() as ses, ses.begin():
        owner = User(
            username=f"hooks_oauth_{overrides.pop('username_suffix', 'owner')}",
            role="super_admin",
            password_hash="$argon2id$v=19$m=65536,t=3,p=4$abc$def",
            password_reset_required=False,
        )
        ses.add(owner)
        await ses.flush()
        new_id = await MailAccountsRepo(ses).next_account_id()
        acc = MailAccount(
            id=new_id,
            user_id=owner.id,
            email=f"oauth{new_id}@example.com",
            auth_type="oauth_outlook",
            oauth_provider="outlook",  # CHECK ck_mail_accounts_oauth_creds
            oauth_refresh_token_encrypted=encrypt_mail_password("refresh-token", new_id),
            encrypted_password=None,
            imap_host="outlook.office365.com",
            imap_port=993,
            imap_ssl=True,
            smtp_host="smtp.office365.com",
            smtp_port=587,
            smtp_ssl=False,
            smtp_starttls=True,
            **overrides,
        )
        ses.add(acc)
        await ses.flush()
        return int(acc.id)


async def _load(db_engine: AsyncEngine, account_id: int) -> MailAccount:
    async with _factory(db_engine)() as ses:
        acc = await ses.get(MailAccount, account_id)
    assert acc is not None
    return acc


async def _status_events() -> list[int]:
    """``mail_account_id`` of every item currently on ``crm_status_queue``."""
    raw = await get_redis().lrange(CRM_STATUS_QUEUE_KEY, 0, -1)
    out: list[int] = []
    for item in raw:
        decoded = item.decode() if isinstance(item, bytes) else item
        acc_id = parse_status_payload(decoded)
        assert acc_id is not None
        out.append(acc_id)
    return out


def _spy_hook(monkeypatch: pytest.MonkeyPatch, module: Any) -> list[dict[str, Any]]:
    """Wrap ``enqueue_crm_status_best_effort`` in ``module``, snapshotting the DB row.

    The snapshot is read through a **separate** session (``make_session``), so a
    mirrored field can only show its POST-state when the writing transaction has
    already COMMITted (ADR-0046 §2). An enqueue from inside the open transaction
    would hand us the pre-commit state and the assertions below would fail.
    """
    seen: list[dict[str, Any]] = []
    original = module.enqueue_crm_status_best_effort

    async def _spy(account_id: int) -> None:
        from shared.db import make_session

        async with make_session() as s:
            acc = await s.get(MailAccount, account_id)
        seen.append(
            {
                "account_id": account_id,
                "is_active": None if acc is None else acc.is_active,
                "last_sync_error": None if acc is None else acc.last_sync_error,
                "consecutive_failures": None if acc is None else acc.consecutive_failures,
                "last_synced_at": None if acc is None else acc.last_synced_at,
                "oauth_needs_consent": None if acc is None else acc.oauth_needs_consent,
                "disabled_alert_sent_at": None if acc is None else acc.disabled_alert_sent_at,
            }
        )
        await original(account_id)

    monkeypatch.setattr(module, "enqueue_crm_status_best_effort", _spy)
    return seen


def _patch_fetch(monkeypatch: pytest.MonkeyPatch, result: Callable[[], Any]) -> None:
    """Replace the blocking IMAP fetch (``asyncio.to_thread``) with ``result()``.

    Only ``fetch_blocking`` is faked. Since TD-056 the SSRF guard also runs
    through ``asyncio.to_thread`` (``assert_public_host_async`` — ADR-0047 §4),
    so a blanket fake would hijack the resolve leg as well and feed it
    ``result()`` (or raise the fetch's error) before the cycle ever reaches IMAP.
    Everything that is not the blocking fetch goes to the REAL ``to_thread``.
    """
    real_to_thread = asyncio.to_thread

    async def _fake_to_thread(_func: Any, *_a: Any, **_k: Any) -> Any:
        if getattr(_func, "__name__", "") != "fetch_blocking":
            return await real_to_thread(_func, *_a, **_k)
        return result()

    monkeypatch.setattr(asyncio, "to_thread", _fake_to_thread)


def _empty_box() -> FetchedBox:
    return FetchedBox(uidvalidity=1, uidnext=1, new_messages=[])


async def _run_one(db_engine: AsyncEngine, account_id: int) -> Any:
    acc = await _load(db_engine, account_id)
    return await sc.sync_one_account(
        acc,
        timeout_seconds=10,
        initial_sync_days=30,
        max_body_bytes=1024,
    )


# ---------------------------------------------------------------------------
# H1 — successful cycle (mark_sync_success)
# ---------------------------------------------------------------------------


class TestH1SyncSuccess:
    async def test_success_enqueues_exactly_one_status_event_after_commit(
        self,
        db_engine: AsyncEngine,
        crm_status_on: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        account_id = await _seed_account(
            db_engine,
            username_suffix="h1",
            last_sync_error="network: boom",
            consecutive_failures=2,
        )
        seen = _spy_hook(monkeypatch, sc)
        _patch_fetch(monkeypatch, _empty_box)

        result = await _run_one(db_engine, account_id)
        assert result.outcome == "ok"

        assert await _status_events() == [account_id]
        # §2 — the enqueue saw the COMMITted post-state (success snapshot).
        assert len(seen) == 1
        assert seen[0]["last_sync_error"] is None
        assert seen[0]["consecutive_failures"] == 0
        assert seen[0]["last_synced_at"] is not None


# ---------------------------------------------------------------------------
# H2 — _record_transient (TRANSIENT + PERMANENT phase 0)
# ---------------------------------------------------------------------------


class TestH2RecordTransient:
    async def test_transient_error_enqueues_status_after_commit(
        self,
        db_engine: AsyncEngine,
        crm_status_on: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # ``last_synced_at`` is NULL → suppression is off (ADR-0026 §2).
        account_id = await _seed_account(db_engine, username_suffix="h2t")
        seen = _spy_hook(monkeypatch, sc)

        def _boom() -> FetchedBox:
            raise ConnectionError("connection refused")

        _patch_fetch(monkeypatch, _boom)

        result = await _run_one(db_engine, account_id)
        assert result.outcome == "transient"

        assert await _status_events() == [account_id]
        assert len(seen) == 1
        assert seen[0]["last_sync_error"] is not None
        assert seen[0]["last_sync_error"].startswith("network:")
        # TRANSIENT never bumps the counter nor disables.
        assert seen[0]["consecutive_failures"] == 0
        assert seen[0]["is_active"] is True

    async def test_permanent_phase0_enqueues_status_before_the_phase2_bump(
        self,
        db_engine: AsyncEngine,
        crm_status_on: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``sync_one_account`` alone (no phase 2) → exactly the phase-0 H2 event."""
        account_id = await _seed_account(db_engine, username_suffix="h2p")
        seen = _spy_hook(monkeypatch, sc)

        def _boom() -> FetchedBox:
            raise RuntimeError("AUTHENTICATIONFAILED invalid credentials")

        _patch_fetch(monkeypatch, _boom)

        result = await _run_one(db_engine, account_id)
        assert result.outcome == "permanent"

        assert await _status_events() == [account_id]
        assert len(seen) == 1
        assert seen[0]["last_sync_error"].startswith("auth_failed:")
        assert seen[0]["consecutive_failures"] == 0  # bump is phase 2 (H3)

    async def test_unexpected_exception_in_gather_enqueues_status_after_commit(
        self,
        db_engine: AsyncEngine,
        crm_status_on: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """H2's third call-site: an exception escaping ``sync_one_account`` itself
        (``_run_for_accounts`` phase 1, ``return_exceptions=True``) is recorded
        fail-open as a transient — and therefore mirrored too."""
        account_id = await _seed_account(db_engine, username_suffix="h2g")
        seen = _spy_hook(monkeypatch, sc)

        async def _crash(*_a: Any, **_k: Any) -> Any:
            raise ValueError("runner exploded")

        monkeypatch.setattr(sc, "sync_one_account", _crash)

        acc = await _load(db_engine, account_id)
        ok, failed, new_msgs = await sc._run_for_accounts([acc])
        assert (ok, failed, new_msgs) == (0, 1, 0)

        assert await _status_events() == [account_id]
        assert len(seen) == 1
        # Fail-open transient: error written, counter NOT bumped, box NOT disabled.
        assert seen[0]["last_sync_error"].startswith("error: ValueError")
        assert seen[0]["consecutive_failures"] == 0
        assert seen[0]["is_active"] is True


# ---------------------------------------------------------------------------
# H3 — _record_failure (phase 2 bump)
# ---------------------------------------------------------------------------


class TestH3RecordFailure:
    async def test_failure_bump_enqueues_status_after_commit(
        self,
        db_engine: AsyncEngine,
        crm_status_on: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        account_id = await _seed_account(db_engine, username_suffix="h3")
        seen = _spy_hook(monkeypatch, sc)

        failures = await sc._record_failure(account_id, error="auth_failed: bad", disable=False)
        assert failures == 1

        assert await _status_events() == [account_id]
        assert len(seen) == 1
        # §2 — the committed bump is already visible to the dispatcher's snapshot.
        assert seen[0]["consecutive_failures"] == 1
        assert seen[0]["last_sync_error"] == "auth_failed: bad"

    async def test_failure_bump_does_not_stamp_last_synced_at(
        self,
        db_engine: AsyncEngine,
        crm_status_on: None,
    ) -> None:
        """ADR-0046 §1 — ``last_synced_at`` = last SUCCESSFUL sync, and no
        error branch writes it."""
        stale = _dt.datetime.now(_dt.UTC) - _dt.timedelta(minutes=90)
        account_id = await _seed_account(db_engine, username_suffix="h3ls", last_synced_at=stale)

        await sc._record_failure(account_id, error="auth_failed: bad", disable=False)

        acc = await _load(db_engine, account_id)
        assert acc.last_synced_at is not None
        assert abs((acc.last_synced_at - stale).total_seconds()) < 1


# ---------------------------------------------------------------------------
# H4 — _disable_after_failures (auto-disable)
# ---------------------------------------------------------------------------


class TestH4DisableAfterFailures:
    async def test_auto_disable_enqueues_status_with_committed_is_active_false(
        self,
        db_engine: AsyncEngine,
        crm_status_on: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        account_id = await _seed_account(db_engine, username_suffix="h4", consecutive_failures=3)
        acc = await _load(db_engine, account_id)
        seen = _spy_hook(monkeypatch, sc)

        await sc._disable_after_failures(account_id, user_id=acc.user_id, reason="auth_failed")

        assert await _status_events() == [account_id]
        assert len(seen) == 1
        # §2 — post-COMMIT snapshot: the mailbox is already disabled.
        assert seen[0]["is_active"] is False
        assert seen[0]["disabled_alert_sent_at"] is not None


# ---------------------------------------------------------------------------
# H2+H3+H4 end-to-end through the real cycle (ADR-0046 §2: up to 3 events)
# ---------------------------------------------------------------------------


class TestPermanentCycleEndToEnd:
    async def test_permanent_account_emits_phase0_bump_and_disable_events(
        self,
        db_engine: AsyncEngine,
        crm_status_on: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("SYNC_MASS_FAILURE_MIN", "10")  # breaker must NOT trip
        get_settings.cache_clear()
        account_id = await _seed_account(db_engine, username_suffix="e2e")
        seen = _spy_hook(monkeypatch, sc)

        def _boom() -> FetchedBox:
            raise RuntimeError("AUTHENTICATIONFAILED invalid credentials")

        _patch_fetch(monkeypatch, _boom)

        acc = await _load(db_engine, account_id)
        ok, failed, _ = await sc._run_for_accounts([acc])
        assert (ok, failed) == (0, 1)

        # H2 (phase 0) + H3 (bump) + H4 (disable) = 3 events, all for this box.
        assert await _status_events() == [account_id, account_id, account_id]
        assert [s["consecutive_failures"] for s in seen] == [0, 1, 1]
        assert [s["is_active"] for s in seen] == [True, True, False]

        final = await _load(db_engine, account_id)
        assert final.is_active is False
        # N6 — the production call-site passes ``disable=False``; the disable goes
        # through H4 (``disable_and_stamp_alert``), which is the ONLY writer of
        # the alert stamp. A ``mark_sync_failure(disable=True)`` disable would
        # have left it NULL.
        assert final.disabled_alert_sent_at is not None


# ---------------------------------------------------------------------------
# H7a — transition into oauth_needs_consent (Microsoft invalid_grant)
# ---------------------------------------------------------------------------


class TestH7aNeedsConsentTransition:
    async def test_invalid_grant_marks_and_enqueues_status_after_commit(
        self,
        db_engine: AsyncEngine,
        crm_status_on: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        account_id = await _seed_oauth_account(db_engine, username_suffix="h7a")

        from backend.app.oauth import service as oauth_service

        seen = _spy_hook(monkeypatch, oauth_service)

        async def _refresh_invalid(self: Any, _refresh_token: str) -> Any:
            raise OAuthRefreshInvalidError("invalid_grant")

        monkeypatch.setattr(_TokenClient, "refresh", _refresh_invalid)

        # Drive the real worker path: token refresh → invalid_grant → H7a.
        result = await _run_one(db_engine, account_id)
        assert result.outcome == "ok"  # clean skip — no failure recorded
        assert result.new_count == 0

        assert await _status_events() == [account_id]
        assert len(seen) == 1
        # §2 — post-COMMIT snapshot carries the flag AND the marker.
        assert seen[0]["oauth_needs_consent"] is True
        assert seen[0]["last_sync_error"] == OAUTH_NEEDS_CONSENT_SYNC_ERROR
        # ADR-0025 §3 step 5 — needs-consent never disables / never bumps.
        assert seen[0]["is_active"] is True
        assert seen[0]["consecutive_failures"] == 0


# ---------------------------------------------------------------------------
# H7b — clean-skip branch of an already-needs-consent mailbox (+ idempotency)
# ---------------------------------------------------------------------------


class TestH7bCleanSkipMarker:
    async def test_clean_skip_writes_marker_and_enqueues_status_once(
        self,
        db_engine: AsyncEngine,
        crm_status_on: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The mailbox that never returns to the transition point (H7a) must stop
        mirroring as green: the clean-skip branch stamps the marker + pushes."""
        account_id = await _seed_oauth_account(
            db_engine, username_suffix="h7b", oauth_needs_consent=True
        )
        seen = _spy_hook(monkeypatch, sc)

        result = await _run_one(db_engine, account_id)
        assert result.outcome == "ok"  # clean skip

        assert await _status_events() == [account_id]
        assert len(seen) == 1
        assert seen[0]["last_sync_error"] == OAUTH_NEEDS_CONSENT_SYNC_ERROR
        assert seen[0]["is_active"] is True
        assert seen[0]["consecutive_failures"] == 0

        acc = await _load(db_engine, account_id)
        assert acc.last_sync_error == OAUTH_NEEDS_CONSENT_SYNC_ERROR
        assert acc.last_synced_at is None  # §1 — no error branch stamps it

    async def test_second_cycle_is_idempotent_no_write_no_push(
        self,
        db_engine: AsyncEngine,
        crm_status_on: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A dead mailbox must NOT emit a status event every ``SYNC_INTERVAL``."""
        account_id = await _seed_oauth_account(
            db_engine,
            username_suffix="h7bi",
            oauth_needs_consent=True,
            last_sync_error=OAUTH_NEEDS_CONSENT_SYNC_ERROR,
        )
        before = await _load(db_engine, account_id)
        seen = _spy_hook(monkeypatch, sc)

        await _run_one(db_engine, account_id)
        await _run_one(db_engine, account_id)

        assert await _status_events() == []
        assert seen == []
        after = await _load(db_engine, account_id)
        # Guarded UPDATE matched no row → not even ``updated_at`` moved.
        assert after.updated_at == before.updated_at

    async def test_guarded_update_returns_false_when_marker_already_present(
        self,
        db_engine: AsyncEngine,
    ) -> None:
        """Repo-level guard (``last_sync_error IS DISTINCT FROM`` marker)."""
        account_id = await _seed_oauth_account(
            db_engine, username_suffix="h7bg", oauth_needs_consent=True
        )
        async with _factory(db_engine)() as ses, ses.begin():
            assert await MailAccountsRepo(ses).mark_oauth_needs_consent_error(account_id) is True
        async with _factory(db_engine)() as ses, ses.begin():
            assert await MailAccountsRepo(ses).mark_oauth_needs_consent_error(account_id) is False


# ---------------------------------------------------------------------------
# N3 — oauth-token writes never touch a mirrored column → no hook
# ---------------------------------------------------------------------------


class TestN3OauthTokenWrites:
    async def test_token_rotation_and_consent_reset_emit_no_status_event(
        self,
        db_engine: AsyncEngine,
        crm_status_on: None,
    ) -> None:
        account_id = await _seed_oauth_account(
            db_engine,
            username_suffix="n3",
            oauth_needs_consent=True,
            last_sync_error=OAUTH_NEEDS_CONSENT_SYNC_ERROR,
            consecutive_failures=2,
        )
        async with _factory(db_engine)() as ses, ses.begin():
            await MailAccountsRepo(ses).update_oauth_tokens(
                account_id,
                oauth_access_token_encrypted=encrypt_mail_password("at", account_id),
                oauth_access_token_expires_at=_dt.datetime.now(_dt.UTC) + _dt.timedelta(minutes=30),
                oauth_needs_consent=False,
            )

        assert await _status_events() == []
        acc = await _load(db_engine, account_id)
        # Mirrored columns untouched — the box goes green again via H1 only.
        assert acc.last_sync_error == OAUTH_NEEDS_CONSENT_SYNC_ERROR
        assert acc.consecutive_failures == 2
        assert acc.is_active is True


# ---------------------------------------------------------------------------
# N4 — suppressed TRANSIENT: no DB write → no hook
# ---------------------------------------------------------------------------


class TestN4SuppressedTransient:
    async def test_suppressed_transient_writes_nothing_and_pushes_nothing(
        self,
        db_engine: AsyncEngine,
        crm_status_on: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("SYNC_TRANSIENT_SUPPRESS_MINUTES", "60")
        get_settings.cache_clear()
        fresh = _dt.datetime.now(_dt.UTC) - _dt.timedelta(minutes=5)
        account_id = await _seed_account(db_engine, username_suffix="n4", last_synced_at=fresh)
        seen = _spy_hook(monkeypatch, sc)

        def _boom() -> FetchedBox:
            raise ConnectionError("connection refused")

        _patch_fetch(monkeypatch, _boom)

        result = await _run_one(db_engine, account_id)
        assert result.outcome == "transient"

        assert await _status_events() == []
        assert seen == []
        acc = await _load(db_engine, account_id)
        assert acc.last_sync_error is None  # nothing written at all


# ---------------------------------------------------------------------------
# §1 regression — a PERMANENT failure must NOT refresh the suppression window
# ---------------------------------------------------------------------------


class TestSuppressionWindowRegression:
    async def test_transient_after_permanent_is_no_longer_silently_suppressed(
        self,
        db_engine: AsyncEngine,
        crm_status_on: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The original defect: ``mark_sync_failure`` stamped ``last_synced_at=now()``,
        so a mailbox failing PERMANENT looked "freshly synced" and every following
        TRANSIENT error was suppressed → CRM/UI stayed quiet on a dead box."""
        monkeypatch.setenv("SYNC_TRANSIENT_SUPPRESS_MINUTES", "60")
        get_settings.cache_clear()
        stale = _dt.datetime.now(_dt.UTC) - _dt.timedelta(minutes=90)
        account_id = await _seed_account(db_engine, username_suffix="sup", last_synced_at=stale)

        await sc._record_failure(account_id, error="auth_failed: bad", disable=False)
        acc = await _load(db_engine, account_id)
        assert sc._should_suppress_transient(acc.last_synced_at) is False

        await get_redis().delete(CRM_STATUS_QUEUE_KEY)  # drop the H3 event

        def _boom() -> FetchedBox:
            raise ConnectionError("connection refused")

        _patch_fetch(monkeypatch, _boom)
        result = await _run_one(db_engine, account_id)
        assert result.outcome == "transient"

        # The transient error IS written (not suppressed) and IS mirrored.
        after = await _load(db_engine, account_id)
        assert after.last_sync_error is not None
        assert after.last_sync_error.startswith("network:")
        assert await _status_events() == [account_id]


# ---------------------------------------------------------------------------
# N5 — circuit-breaker: bump/disable suppressed → only the phase-0 H2 event
# ---------------------------------------------------------------------------


class TestN5CircuitBreaker:
    async def test_tripped_breaker_emits_only_the_phase0_event(
        self,
        db_engine: AsyncEngine,
        crm_status_on: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("SYNC_MASS_FAILURE_MIN", "1")  # 1 permanent of 1 → trips
        get_settings.cache_clear()
        account_id = await _seed_account(db_engine, username_suffix="n5")
        seen = _spy_hook(monkeypatch, sc)

        def _boom() -> FetchedBox:
            raise RuntimeError("AUTHENTICATIONFAILED invalid credentials")

        _patch_fetch(monkeypatch, _boom)

        acc = await _load(db_engine, account_id)
        await sc._run_for_accounts([acc])

        # Only H2 (phase 0). No H3 bump, no H4 disable.
        assert await _status_events() == [account_id]
        assert len(seen) == 1
        final = await _load(db_engine, account_id)
        assert final.consecutive_failures == 0
        assert final.is_active is True
        assert final.last_sync_error.startswith("auth_failed:")


# ---------------------------------------------------------------------------
# N6 — mark_sync_failure(disable=True): unreachable in prod, still hooked once
# ---------------------------------------------------------------------------


class TestN6FailureWithDisableFlag:
    async def test_disable_true_still_fires_exactly_one_hook_on_the_wrapper(
        self,
        db_engine: AsyncEngine,
        crm_status_on: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        account_id = await _seed_account(db_engine, username_suffix="n6")
        seen = _spy_hook(monkeypatch, sc)

        await sc._record_failure(account_id, error="auth_failed: bad", disable=True)

        assert await _status_events() == [account_id]
        assert len(seen) == 1
        assert seen[0]["is_active"] is False  # hook sits on the wrapper, not the branch
        final = await _load(db_engine, account_id)
        # This path does NOT stamp the alert marker — that is H4's job only.
        assert final.disabled_alert_sent_at is None


# ---------------------------------------------------------------------------
# Best-effort / feature gate (ADR-0046 §2)
# ---------------------------------------------------------------------------


class TestBestEffortAndGate:
    async def test_redis_outage_in_the_hook_never_breaks_the_sync_cycle(
        self,
        db_engine: AsyncEngine,
        crm_status_on: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        account_id = await _seed_account(db_engine, username_suffix="be")

        from backend.app.crm_push import service as crm_service

        def _boom_redis() -> Any:
            raise RuntimeError("simulated redis outage")

        monkeypatch.setattr(crm_service, "get_redis", _boom_redis)
        _patch_fetch(monkeypatch, _empty_box)

        result = await _run_one(db_engine, account_id)
        # The cycle survives; the success is committed.
        assert result.outcome == "ok"
        acc = await _load(db_engine, account_id)
        assert acc.last_synced_at is not None
        assert acc.last_sync_error is None

    async def test_disabled_channel_never_touches_redis(
        self,
        db_engine: AsyncEngine,
        crm_status_off: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        account_id = await _seed_account(db_engine, username_suffix="gate")
        calls: list[int] = []

        from backend.app.crm_push import service as crm_service

        def _tripwire() -> Any:
            calls.append(1)
            raise AssertionError("redis must not be touched when crm_status is disabled")

        monkeypatch.setattr(crm_service, "get_redis", _tripwire)
        _patch_fetch(monkeypatch, _empty_box)

        result = await _run_one(db_engine, account_id)
        assert result.outcome == "ok"
        assert calls == []
