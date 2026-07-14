"""ADR-0028 — OAuth IMAP "login failed" => transient, no-disable (live PG).

Integration scope: drives the REAL ``sync_cycle`` two-phase path against a real
Postgres row for an ``oauth_outlook`` account that, AFTER a successful token
refresh, receives an IMAP ``LOGIN failed.`` flake. Asserts the end-to-end
ADR-0028 contract:

* oauth_outlook + IMAP "login failed" -> ``outcome == "transient"``;
  ``consecutive_failures`` is NOT bumped; ``is_active`` stays ``True``; NO
  ``account_auto_disabled`` audit row is written (instant-disable excluded by
  construction — transient + ``explicit_permanent=False``).
* REGRESSION: a ``password`` account with the same IMAP "login failed" is an
  explicit-permanent instant-disable (``is_active=False`` after one cycle, an
  ``account_auto_disabled`` audit with ``reason='auth_failed'``).
* Kill-switch: ``SYNC_OAUTH_LOGIN_FAILED_TRANSIENT=False`` reverts the oauth
  account to the legacy permanent instant-disable.
* Suppress: an oauth flake with a FRESH ``last_synced_at`` (< suppress window)
  leaves ``last_sync_error`` unwritten (ADR-0026 §2 suppression propagates to
  the new transient automatically).

The OAuth refresh (``OutlookTokenService.get_valid_access_token``) is mocked to
return a token (simulating a SUCCESSFUL refresh — the ADR invariant: the token
that reaches IMAP is valid). ``asyncio.to_thread`` is mocked so ``fetch_blocking``
raises the IMAP ``LOGIN failed.`` flake. No real network.

Needs a real Postgres (docker-compose.test.yml); reuses the worker package's
autouse DB/Redis/MinIO truncation fixtures.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import imaplib
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from shared.crypto import encrypt_mail_password
from shared.models import AdminAudit, MailAccount, User
from worker.app import sync_cycle as sc

pytestmark = pytest.mark.integration  # needs the DB + Redis to be live

# The prod-incident IMAP flake (ADR-0028 Context): Microsoft answers a valid
# XOAUTH2 with a spurious LOGIN failure on a healthy mailbox.
_IMAP_LOGIN_FAILED = imaplib.IMAP4.error("b'LOGIN failed.'")


@pytest.fixture
async def oauth_account(db_engine: AsyncEngine) -> dict[str, Any]:
    """Seed a super-admin + one active ``oauth_outlook`` mail_account."""
    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with factory() as ses, ses.begin():
        admin = User(
            username="oauth_admin",
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
            email="oauth@example.com",
            # oauth_outlook: no password; refresh token blob present (CHECK).
            encrypted_password=None,
            auth_type="oauth_outlook",
            oauth_provider="outlook",
            oauth_refresh_token_encrypted=encrypt_mail_password("refresh-tok", new_id),
            oauth_needs_consent=False,
            imap_host="outlook.office365.com",
            imap_port=993,
            imap_ssl=True,
            smtp_host="smtp.office365.com",
            smtp_port=587,
            smtp_ssl=False,
            smtp_starttls=True,
        )
        ses.add(acc)
        await ses.flush()
        return {"user_id": admin.id, "account_id": acc.id}


@pytest.fixture
async def password_account(db_engine: AsyncEngine) -> dict[str, Any]:
    """Seed a super-admin + one active ``password`` mail_account (regression)."""
    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with factory() as ses, ses.begin():
        admin = User(
            username="pwd_admin",
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
            email="pwd@example.com",
            encrypted_password=encrypt_mail_password("p", new_id),
            auth_type="password",
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


async def _auto_disabled_audits(db_engine: AsyncEngine) -> list[AdminAudit]:
    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with factory() as ses:
        rows = (
            (
                await ses.execute(
                    select(AdminAudit).where(AdminAudit.action == "account_auto_disabled")
                )
            )
            .scalars()
            .all()
        )
    return list(rows)


def _patch_imap_login_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``fetch_blocking`` (via ``asyncio.to_thread``) raise the IMAP flake.

    The flake is raised AFTER ``_resolve_oauth_access_token`` has already
    succeeded (token mocked below) — exactly the ADR-0028 sequence: refresh OK,
    then a spurious IMAP login failure.

    Only ``fetch_blocking`` is faked: since TD-056 the SSRF guard also goes
    through ``asyncio.to_thread`` (``assert_public_host_async``, ADR-0047 §4), so
    a blanket fake would hijack the resolve leg too and raise the IMAP flake
    before the cycle ever reaches IMAP. Everything that is not the blocking fetch
    is delegated to the REAL ``asyncio.to_thread``.
    """
    real_to_thread = asyncio.to_thread

    async def _fake_to_thread(_func: Any, *_a: Any, **_k: Any) -> Any:
        if getattr(_func, "__name__", "") != "fetch_blocking":
            return await real_to_thread(_func, *_a, **_k)
        raise _IMAP_LOGIN_FAILED

    monkeypatch.setattr(asyncio, "to_thread", _fake_to_thread)


def _patch_oauth_token_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mock the OAuth refresh to return a valid access token (refresh succeeds —
    the ADR invariant that makes a later IMAP login-failed a flake)."""
    from backend.app.oauth.service import OutlookTokenService

    async def _fake_token(self: Any, account: MailAccount) -> str:
        return "valid-access-token"

    monkeypatch.setattr(OutlookTokenService, "get_valid_access_token", _fake_token)


def _force_kill_switch(monkeypatch: pytest.MonkeyPatch, *, enabled: bool) -> None:
    """Force SYNC_OAUTH_LOGIN_FAILED_TRANSIENT inside sync_cycle only (the rest
    of settings — used by repos/audit — stays the real value)."""
    real = sc.get_settings()

    class _S:
        def __getattr__(self, name: str) -> Any:
            if name == "SYNC_OAUTH_LOGIN_FAILED_TRANSIENT":
                return enabled
            return getattr(real, name)

    monkeypatch.setattr(sc, "get_settings", lambda: _S())


class TestOAuthLoginFailedTransient:
    async def test_oauth_login_failed_is_transient_no_disable(
        self,
        oauth_account: dict[str, Any],
        db_engine: AsyncEngine,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """oauth_outlook + IMAP login failed -> transient; counter untouched;
        account stays active; NO auto-disable audit. Runs the FULL two-phase
        cycle (single account, breaker not engaged)."""
        _patch_oauth_token_ok(monkeypatch)
        _patch_imap_login_failed(monkeypatch)
        account_id = oauth_account["account_id"]
        # Stale last_synced_at so the transient error IS written (we want to
        # assert the calm ``network:`` prefix, not suppression — suppression has
        # its own test below).
        from backend.app.repositories.mail_accounts import MailAccountsRepo

        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as ses, ses.begin():
            await MailAccountsRepo(ses).update_fields(
                account_id, last_synced_at=_dt.datetime.now(_dt.UTC) - _dt.timedelta(hours=10)
            )

        acc = await _reload(db_engine, account_id)
        await sc._run_for_accounts([acc])

        row = await _reload(db_engine, account_id)
        assert row.is_active is True  # transient never disables
        assert row.consecutive_failures == 0  # transient never bumps
        assert row.last_sync_error is not None
        # Calm ``network`` prefix, NOT the scary ``auth_failed`` (rule 7b).
        assert row.last_sync_error.startswith("network:")
        assert await _auto_disabled_audits(db_engine) == []

    async def test_oauth_login_failed_repeated_never_disables(
        self,
        oauth_account: dict[str, Any],
        db_engine: AsyncEngine,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Three consecutive oauth login-failed cycles must NEVER disable — the
        ADR-0028 root-cause fix (pre-fix this disabled the mailbox on cycle 1)."""
        _patch_oauth_token_ok(monkeypatch)
        _patch_imap_login_failed(monkeypatch)
        account_id = oauth_account["account_id"]

        for _ in range(3):
            acc = await _reload(db_engine, account_id)
            await sc._run_for_accounts([acc])

        row = await _reload(db_engine, account_id)
        assert row.is_active is True
        assert row.consecutive_failures == 0
        assert await _auto_disabled_audits(db_engine) == []

    async def test_oauth_login_failed_suppressed_when_fresh(
        self,
        oauth_account: dict[str, Any],
        db_engine: AsyncEngine,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Fresh last_synced_at (< suppress window) -> the transient oauth flake
        is hidden from the UI (``last_sync_error`` stays NULL). ADR-0028 §6 —
        suppression propagates automatically to the new transient."""
        _patch_oauth_token_ok(monkeypatch)
        _patch_imap_login_failed(monkeypatch)
        account_id = oauth_account["account_id"]
        from backend.app.repositories.mail_accounts import MailAccountsRepo

        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as ses, ses.begin():
            await MailAccountsRepo(ses).update_fields(
                account_id, last_synced_at=_dt.datetime.now(_dt.UTC) - _dt.timedelta(minutes=2)
            )

        acc = await _reload(db_engine, account_id)
        # SYNC_TRANSIENT_SUPPRESS_MINUTES default is 60 (> 2 min) so suppressed.
        from shared.config import get_settings

        get_settings.cache_clear()
        assert get_settings().SYNC_TRANSIENT_SUPPRESS_MINUTES >= 3

        await sc._run_for_accounts([acc])

        row = await _reload(db_engine, account_id)
        assert row.last_sync_error is None  # suppressed
        assert row.consecutive_failures == 0
        assert row.is_active is True

    async def test_kill_switch_off_oauth_login_failed_instant_disables(
        self,
        oauth_account: dict[str, Any],
        db_engine: AsyncEngine,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """SYNC_OAUTH_LOGIN_FAILED_TRANSIENT=False reverts the oauth account to
        the legacy permanent instant-disable (kill-switch escape hatch)."""
        _patch_oauth_token_ok(monkeypatch)
        _patch_imap_login_failed(monkeypatch)
        _force_kill_switch(monkeypatch, enabled=False)
        account_id = oauth_account["account_id"]

        acc = await _reload(db_engine, account_id)
        await sc._run_for_accounts([acc])

        row = await _reload(db_engine, account_id)
        assert row.is_active is False  # legacy permanent instant-disable
        assert row.last_sync_error is not None
        assert row.last_sync_error.startswith("auth_failed:")
        audits = await _auto_disabled_audits(db_engine)
        assert len(audits) == 1
        assert audits[0].details["reason"] == "auth_failed"


class TestPasswordLoginFailedRegression:
    async def test_password_login_failed_instant_disables(
        self,
        password_account: dict[str, Any],
        db_engine: AsyncEngine,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """REGRESSION: a password account with IMAP login failed is an explicit
        permanent (rule 8) -> instant disable + ``auth_failed`` audit. ADR-0028
        leaves the password path completely unchanged."""
        _patch_imap_login_failed(monkeypatch)
        account_id = password_account["account_id"]

        acc = await _reload(db_engine, account_id)
        await sc._run_for_accounts([acc])

        row = await _reload(db_engine, account_id)
        assert row.is_active is False  # explicit-permanent => instant disable
        assert row.consecutive_failures == 1  # bumped once before disable
        assert row.last_sync_error is not None
        assert row.last_sync_error.startswith("auth_failed:")
        audits = await _auto_disabled_audits(db_engine)
        assert len(audits) == 1
        assert audits[0].details["reason"] == "auth_failed"
