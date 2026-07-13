"""ADR-0046 — mailbox status-channel: backend-API hook points + negative requirements.

Source of truth: ``docs/adr/ADR-0046-mailbox-status-hook-points.md`` (§2 invariant
"enqueue strictly AFTER COMMIT", §3 H5/H6, §4 N1/N2/N7, §5 per-point coverage).
The worker-side points (H1-H4, H7a, H7b, N3-N6) live in the sibling module
``test_crm_status_hooks_adr0046.py``.

Covered:

- **H5** ``PATCH /api/mail-accounts/{id}`` with new credentials (re-enable branch of
  ``MailAccountService.update``) → exactly one ``crm_status_queue`` event, enqueued
  AFTER the COMMIT;
- **H6** ``PATCH /api/external/mailboxes/{id}`` with ``is_active`` (``set_active``) —
  both deactivate and activate;
- **§2 ordering**: the hook spy reads the row through a SEPARATE session, so the
  POST-state can only be visible once the request transaction has committed. The
  service defers the enqueue (``_pending_status_account_ids``) and the ROUTER
  flushes it outside ``async with db.begin():``;
- **rollback**: a ``DomainError`` inside ``db.begin()`` (409 on a credential PATCH)
  → the transaction rolls back and the flush is never reached → NO event;
- **N1** create, **N2** delete, **N7** ``update_fields`` call-sites that do not write a
  mirrored column (oauth display-name edit; bare display-name edit on a password box);
- best-effort: a Redis outage inside ``flush_crm_status_events`` does NOT fail the
  already-committed PATCH; with ``crm_status_enabled=false`` Redis is never touched.

This module lives under ``tests/worker`` because that package (and NOT ``tests/unit``)
carries the DB/Redis/MinIO cleanup fixtures and is inside the CI test scope
(``.github/workflows/ci.yml``: ``tests/unit tests/worker tests/frontend``).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import structlog
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from backend.app.accounts import service as accounts_service
from backend.app.accounts.service import MailAccountService
from backend.app.crm_push.service import CRM_STATUS_QUEUE_KEY, parse_status_payload
from backend.app.deps import VisibilityScope
from backend.app.repositories.mail_accounts import MailAccountsRepo
from shared.config import get_settings
from shared.crypto import encrypt_mail_password
from shared.models import MailAccount, User
from shared.redis_client import get_redis
from tests.integration.conftest import login_as_admin

pytestmark = pytest.mark.integration  # needs DB + Redis + MinIO (app lifespan)

_API_KEY = "test-external-write-key"


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _structlog_isolation() -> Iterator[None]:
    """Do not leak the app's global structlog configuration out of this module.

    Driving the HTTP surface boots the FastAPI app, whose lifespan calls
    ``configure_logging()`` (``shared/logging.py:147`` — ``cache_logger_on_first_use=True``).
    That setting is **process-global**: once it is on, ``log.bind(...)`` returns an
    eagerly-bound logger whose processor chain is frozen at bind time, so a later
    ``structlog.testing.capture_logs()`` can no longer intercept it. Suites that bind
    their logger *before* entering ``capture_logs`` (e.g.
    ``test_sync_error_observability_adr0026.py:118``) would then silently capture ZERO
    events and fail — green alone, red in the full run.

    Snapshot the config before each test and restore it afterwards, so the state this
    module inherits is exactly the state it hands on.
    """
    saved = structlog.get_config()
    yield
    structlog.configure(**saved)


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
def crm_status_off(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv("CRM_MAILBOX_STATUS_URL", raising=False)
    monkeypatch.delenv("CRM_PUSH_SECRET", raising=False)
    monkeypatch.setenv("EXTERNAL_API_KEY", _API_KEY)
    monkeypatch.setenv("EXTERNAL_WRITE_ENABLED", "true")
    get_settings.cache_clear()
    assert get_settings().crm_status_enabled is False
    yield
    get_settings.cache_clear()


@pytest.fixture
def no_imap_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the IMAP/SMTP connectivity probe (external boundary)."""

    async def _fake_test(self: Any, payload: Any, *, scope: Any = None) -> Any:
        from backend.app.accounts.schemas import TestResult

        return TestResult(imap_ok=True, smtp_ok=True)

    monkeypatch.setattr(MailAccountService, "test", _fake_test)


def _factory(db_engine: AsyncEngine) -> async_sessionmaker[Any]:
    return async_sessionmaker(bind=db_engine, expire_on_commit=False)


async def _seed_account(db_engine: AsyncEngine, **overrides: Any) -> int:
    """Seed an owner + one mailbox (defaults: disabled with a failure history)."""
    suffix = overrides.pop("username_suffix", "owner")
    async with _factory(db_engine)() as ses, ses.begin():
        owner = User(
            username=f"api_hooks_{suffix}",
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
            email=f"apihooks{new_id}@example.com",
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


async def _load(db_engine: AsyncEngine, account_id: int) -> MailAccount | None:
    async with _factory(db_engine)() as ses:
        return await ses.get(MailAccount, account_id)


async def _status_events() -> list[int]:
    raw = await get_redis().lrange(CRM_STATUS_QUEUE_KEY, 0, -1)
    out: list[int] = []
    for item in raw:
        decoded = item.decode() if isinstance(item, bytes) else item
        acc_id = parse_status_payload(decoded)
        assert acc_id is not None
        out.append(acc_id)
    return out


def _spy_hook(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Snapshot the row (via a SEPARATE session) at the moment the hook fires.

    ADR-0046 §2: the hook must run after the COMMIT. A separate session cannot
    see uncommitted data, so a POST-state snapshot here IS the ordering proof.
    """
    seen: list[dict[str, Any]] = []
    original = accounts_service.enqueue_crm_status_best_effort

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
                "disabled_alert_sent_at": None if acc is None else acc.disabled_alert_sent_at,
            }
        )
        await original(account_id)

    monkeypatch.setattr(accounts_service, "enqueue_crm_status_best_effort", _spy)
    return seen


def _key_headers() -> dict[str, str]:
    return {"X-API-Key": _API_KEY, "Content-Type": "application/json"}


# ---------------------------------------------------------------------------
# H5 — PATCH /api/mail-accounts/{id} with new credentials (re-enable)
# ---------------------------------------------------------------------------


class TestH5CredentialReEnable:
    async def test_creds_change_enqueues_one_event_with_the_committed_post_state(
        self,
        db_engine: AsyncEngine,
        crm_status_on: None,
        no_imap_probe: None,
        client: httpx.AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        account_id = await _seed_account(
            db_engine,
            username_suffix="h5",
            is_active=False,
            consecutive_failures=3,
            last_sync_error="auth_failed: bad password",
        )
        csrf = await login_as_admin(client)
        seen = _spy_hook(monkeypatch)

        resp = await client.patch(
            f"/api/mail-accounts/{account_id}",
            json={"password": "new-app-password"},
            headers={"X-CSRF-Token": csrf, "Content-Type": "application/json"},
        )
        assert resp.status_code == 200, resp.text

        assert await _status_events() == [account_id]
        assert len(seen) == 1
        # §2 — the enqueue observed the COMMITted re-enable, not the pre-state.
        assert seen[0]["is_active"] is True
        assert seen[0]["consecutive_failures"] == 0
        assert seen[0]["last_sync_error"] is None
        assert seen[0]["disabled_alert_sent_at"] is None

    async def test_rollback_on_domain_error_emits_no_event(
        self,
        db_engine: AsyncEngine,
        crm_status_on: None,
        no_imap_probe: None,
        client: httpx.AsyncClient,
    ) -> None:
        """409 raised inside ``db.begin()`` → rollback → the router never reaches
        ``flush_crm_status_events`` → nothing on the queue."""
        account_id = await _seed_account(
            db_engine,
            username_suffix="h5rb",
            is_active=False,
            consecutive_failures=3,
            last_sync_error="auth_failed: bad password",
        )
        csrf = await login_as_admin(client)

        resp = await client.patch(
            f"/api/mail-accounts/{account_id}",
            json={"password": "new-app-password", "smtp_ssl": True, "smtp_starttls": True},
            headers={"X-CSRF-Token": csrf, "Content-Type": "application/json"},
        )
        assert resp.status_code == 409, resp.text

        assert await _status_events() == []
        acc = await _load(db_engine, account_id)
        assert acc is not None
        # Nothing was mirrored because nothing was committed.
        assert acc.is_active is False
        assert acc.consecutive_failures == 3
        assert acc.last_sync_error == "auth_failed: bad password"


# ---------------------------------------------------------------------------
# H6 — PATCH /api/external/mailboxes/{id} (set_active)
# ---------------------------------------------------------------------------


class TestH6SetActive:
    async def test_deactivate_enqueues_one_event_with_committed_is_active_false(
        self,
        db_engine: AsyncEngine,
        crm_status_on: None,
        client: httpx.AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The one status change that can never be re-derived — a deactivated box
        leaves ``list_active()`` and never syncs again."""
        account_id = await _seed_account(db_engine, username_suffix="h6off")
        seen = _spy_hook(monkeypatch)

        resp = await client.patch(
            f"/api/external/mailboxes/{account_id}",
            json={"is_active": False},
            headers=_key_headers(),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["is_active"] is False

        assert await _status_events() == [account_id]
        assert len(seen) == 1
        # §2 — post-COMMIT snapshot: the dispatcher would push ``is_active=false``.
        assert seen[0]["is_active"] is False

        acc = await _load(db_engine, account_id)
        assert acc is not None
        assert acc.is_active is False

    async def test_activate_enqueues_one_event_and_clears_the_error_state(
        self,
        db_engine: AsyncEngine,
        crm_status_on: None,
        client: httpx.AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        account_id = await _seed_account(
            db_engine,
            username_suffix="h6on",
            is_active=False,
            consecutive_failures=3,
            last_sync_error="auth_failed: bad password",
        )
        seen = _spy_hook(monkeypatch)

        resp = await client.patch(
            f"/api/external/mailboxes/{account_id}",
            json={"is_active": True},
            headers=_key_headers(),
        )
        assert resp.status_code == 200, resp.text

        assert await _status_events() == [account_id]
        assert len(seen) == 1
        assert seen[0]["is_active"] is True
        assert seen[0]["consecutive_failures"] == 0
        assert seen[0]["last_sync_error"] is None


# ---------------------------------------------------------------------------
# N1 / N2 — create + delete never mirror (the CRM is the initiator)
# ---------------------------------------------------------------------------


class TestN1CreateNoHook:
    async def test_external_create_emits_no_status_event(
        self,
        crm_status_on: None,
        no_imap_probe: None,
        client: httpx.AsyncClient,
    ) -> None:
        resp = await client.post(
            "/api/external/mailboxes",
            json={
                "email": "created@example.com",
                "password": "pw",
                "imap_host": "imap.example.com",
                "imap_port": 993,
                "imap_ssl": True,
                "smtp_host": "smtp.example.com",
                "smtp_port": 465,
                "smtp_ssl": True,
                "smtp_starttls": False,
            },
            headers=_key_headers(),
        )
        assert resp.status_code == 201, resp.text
        assert await _status_events() == []


class TestN2DeleteNoHook:
    async def test_external_delete_emits_no_status_event(
        self,
        db_engine: AsyncEngine,
        crm_status_on: None,
        client: httpx.AsyncClient,
    ) -> None:
        account_id = await _seed_account(db_engine, username_suffix="n2")

        resp = await client.delete(
            f"/api/external/mailboxes/{account_id}",
            headers={"X-API-Key": _API_KEY},
        )
        assert resp.status_code == 204, resp.text

        assert await _status_events() == []
        assert await _load(db_engine, account_id) is None


# ---------------------------------------------------------------------------
# N7 — update_fields call-sites that write no mirrored column
# ---------------------------------------------------------------------------


class TestN7NonStatusUpdateFields:
    async def test_bare_display_name_edit_keeps_status_and_emits_no_event(
        self,
        db_engine: AsyncEngine,
        crm_status_on: None,
        no_imap_probe: None,
        client: httpx.AsyncClient,
    ) -> None:
        """``creds_changed == false`` → no ``is_active`` / counter reset → no hook."""
        account_id = await _seed_account(
            db_engine,
            username_suffix="n7b",
            is_active=False,
            consecutive_failures=2,
            last_sync_error="auth_failed: bad password",
        )
        csrf = await login_as_admin(client)

        resp = await client.patch(
            f"/api/mail-accounts/{account_id}",
            json={"display_name": "Renamed box"},
            headers={"X-CSRF-Token": csrf, "Content-Type": "application/json"},
        )
        assert resp.status_code == 200, resp.text

        assert await _status_events() == []
        acc = await _load(db_engine, account_id)
        assert acc is not None
        assert acc.display_name == "Renamed box"
        # The mirrored columns are untouched — the box stays disabled/failing.
        assert acc.is_active is False
        assert acc.consecutive_failures == 2
        assert acc.last_sync_error == "auth_failed: bad password"

    async def test_oauth_display_name_edit_emits_no_event(
        self,
        db_engine: AsyncEngine,
        crm_status_on: None,
        app: Any,
    ) -> None:
        """The oauth branch of ``MailAccountService.update`` writes only
        ``display_name`` (ADR-0046 §4 N7a)."""
        async with _factory(db_engine)() as ses, ses.begin():
            owner = User(
                username="api_hooks_n7a",
                role="super_admin",
                password_hash="$argon2id$v=19$m=65536,t=3,p=4$abc$def",
                password_reset_required=False,
            )
            ses.add(owner)
            await ses.flush()
            new_id = await MailAccountsRepo(ses).next_account_id()
            ses.add(
                MailAccount(
                    id=new_id,
                    user_id=owner.id,
                    email=f"oauthn7a{new_id}@example.com",
                    auth_type="oauth_outlook",
                    oauth_provider="outlook",
                    oauth_refresh_token_encrypted=encrypt_mail_password("rt", new_id),
                    encrypted_password=None,
                    consecutive_failures=2,
                    last_sync_error="auth_failed: bad password",
                    imap_host="outlook.office365.com",
                    imap_port=993,
                    imap_ssl=True,
                    smtp_host="smtp.office365.com",
                    smtp_port=587,
                    smtp_ssl=False,
                    smtp_starttls=True,
                )
            )
            await ses.flush()
            account_id = int(new_id)
            owner_id = int(owner.id)

        from backend.app.accounts.schemas import MailAccountUpdateRequest

        scope = VisibilityScope(
            user_id=owner_id,
            role="super_admin",
            group_id=None,
            group_ids=frozenset(),
        )
        async with _factory(db_engine)() as ses:
            service = MailAccountService(ses)
            async with ses.begin():
                await service.update(
                    scope=scope,
                    account_id=account_id,
                    payload=MailAccountUpdateRequest(display_name="Outlook box"),
                )
            await service.flush_crm_status_events()

        assert await _status_events() == []
        acc = await _load(db_engine, account_id)
        assert acc is not None
        assert acc.display_name == "Outlook box"
        assert acc.consecutive_failures == 2
        assert acc.last_sync_error == "auth_failed: bad password"


# ---------------------------------------------------------------------------
# Best-effort + feature gate (ADR-0046 §2)
# ---------------------------------------------------------------------------


class TestBestEffortAndGate:
    async def test_redis_outage_in_flush_does_not_fail_the_committed_patch(
        self,
        db_engine: AsyncEngine,
        crm_status_on: None,
        client: httpx.AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        account_id = await _seed_account(db_engine, username_suffix="be")

        from backend.app.crm_push import service as crm_service

        def _boom_redis() -> Any:
            raise RuntimeError("simulated redis outage")

        monkeypatch.setattr(crm_service, "get_redis", _boom_redis)

        resp = await client.patch(
            f"/api/external/mailboxes/{account_id}",
            json={"is_active": False},
            headers=_key_headers(),
        )
        # The PATCH is already committed — a failed enqueue must not undo it.
        assert resp.status_code == 200, resp.text
        acc = await _load(db_engine, account_id)
        assert acc is not None
        assert acc.is_active is False

    async def test_disabled_channel_never_touches_redis(
        self,
        db_engine: AsyncEngine,
        crm_status_off: None,
        client: httpx.AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        account_id = await _seed_account(db_engine, username_suffix="gate")
        calls: list[int] = []

        from backend.app.crm_push import service as crm_service

        def _tripwire() -> Any:
            calls.append(1)
            raise AssertionError("redis must not be touched when crm_status is disabled")

        monkeypatch.setattr(crm_service, "get_redis", _tripwire)

        resp = await client.patch(
            f"/api/external/mailboxes/{account_id}",
            json={"is_active": False},
            headers=_key_headers(),
        )
        assert resp.status_code == 200, resp.text
        assert calls == []
