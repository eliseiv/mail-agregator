"""ADR-0047 — hard-deadline of the mailbox connection-test (TD-055 / TD-056).

Source of truth: ``docs/adr/ADR-0047-mailbox-test-hard-deadline.md`` §1-§5 and
``docs/05-modules.md`` §9.2 / §21.

The norm is proven BEHAVIOURALLY, never by arithmetic over the fail-fast
constants: ``05-modules.md`` §21 explicitly FORBIDS asserting
``_IMAP_TIMEOUT + _SMTP_TIMEOUT <= MAILBOX_TEST_DEADLINE_SECONDS`` — those
socket timeouts apply per operation/phase, so ``40`` bounds nothing (ADR-0047
§2.2). Every test here therefore hangs a REAL mock mail server and MEASURES the
wall-clock time to the domain ``422``.

What is covered (§21):

- probe hangs longer than the deadline → domain ``422`` WITHIN the deadline
  (+ teardown), never a hang, a ``500`` or a ``504``;
- stage attribution by marker: silent IMAP → ``imap_login_failed``/``imap``;
  silent SMTP after a green IMAP → ``smtp_login_failed``/``smtp``; a hung
  refresh exchange on the oauth path → ``imap_login_failed``/``oauth_token``;
- all entrances inherit the deadline: ``POST /mailboxes/test`` (ad-hoc AND the
  ``account_id`` re-probe branch), ``POST /mailboxes`` (mailbox NOT created),
  ``PATCH /mailboxes/{id}`` (credentials NOT changed);
- teardown leg (§2.3): a server that ignores ``QUIT`` too → the response lands
  no later than ``deadline + _SMTP_QUIT_TIMEOUT`` (5 s), NOT ``+ _SMTP_TIMEOUT``
  (20 s) — the leg that keeps ``45 + 5 + 5 = 55 < 60`` true;
- off-loop SSRF resolve (§4): while ``getaddrinfo`` hangs, the event loop stays
  responsive (a concurrent ``GET /healthz`` answers) and the deadline still
  fires.

The deadline is shortened to :data:`TEST_DEADLINE` seconds for the run — that is
CONFIGURATION (the very env the ADR introduces), not a mock of our own code. The
production ``ge=10`` bound is asserted separately in
``tests/unit/test_config_adr0047.py``.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from backend.app.accounts.testers import _SMTP_QUIT_TIMEOUT, _SMTP_TIMEOUT
from tests.conftest import _pg_available, _redis_available
from tests.loop_probe import hung_getaddrinfo
from tests.unit.mailserver_mocks import (
    Endpoint,
    imap_server_ok,
    silent_server,
    smtp_banner_then_silent,
)

pytestmark = pytest.mark.unit

#: Deadline used for the run. Short enough to keep the suite fast, long enough
#: that a slow CI runner cannot mistake scheduling jitter for an expiry.
TEST_DEADLINE = 2

#: Slack added on top of every asserted upper bound to absorb CI jitter.
SLACK = 3.0

API_KEY = "qa-adr0047-external-key"
HEADERS = {"X-API-Key": API_KEY}

_MAILBOX_PASSWORD = "s3cret-pass"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def deadline_settings(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Shorten the deadline and switch the external write API on, for THIS test.

    ``get_settings`` is ``lru_cache``d, so the app and the tests share one
    instance; ``monkeypatch`` restores every attribute at teardown, which keeps
    the tests isolated from each other (no global state leaks into the next).
    """
    from shared.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "MAILBOX_TEST_DEADLINE_SECONDS", TEST_DEADLINE)
    monkeypatch.setattr(s, "EXTERNAL_API_KEY", API_KEY)
    monkeypatch.setattr(s, "EXTERNAL_WRITE_ENABLED", True)
    return s


@pytest_asyncio.fixture
async def crm_db(db_engine: AsyncEngine) -> AsyncIterator[AsyncEngine]:
    """Clean ``mail_accounts`` + the seeded ``crm-service`` owner of the write API.

    The external write API derives the owner from the ``crm-service`` technical
    user, normally seeded in the app lifespan — which the ASGI transport does
    not run. We seed it with the production seeder itself.
    """
    if not _redis_available():
        pytest.skip("redis not reachable — external write API rate-limits via redis")

    from backend.app.auth.service import seed_crm_service_user

    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False, class_=AsyncSession)
    async with db_engine.begin() as conn:
        await conn.execute(text("TRUNCATE TABLE mail_accounts RESTART IDENTITY CASCADE"))
    async with factory() as session, session.begin():
        await seed_crm_service_user(session)
    yield db_engine
    async with db_engine.begin() as conn:
        await conn.execute(text("TRUNCATE TABLE mail_accounts RESTART IDENTITY CASCADE"))


@pytest_asyncio.fixture
async def client(deadline_settings: Any) -> AsyncIterator[httpx.AsyncClient]:
    """Live ASGI app behind httpx (no lifespan — fixtures seed what it would)."""
    from backend.app.main import app

    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


@pytest.fixture(autouse=True)
def _needs_pg() -> None:
    if not _pg_available():
        pytest.skip("postgres not reachable — start the test stack")


def _body(imap: Endpoint, smtp: Endpoint, **extra: Any) -> dict[str, Any]:
    """Plaintext (no TLS) probe payload against the two mock endpoints."""
    return {
        "email": "probe@example.com",
        "password": _MAILBOX_PASSWORD,
        "imap_host": imap[0],
        "imap_port": imap[1],
        "imap_ssl": False,
        "smtp_host": smtp[0],
        "smtp_port": smtp[1],
        "smtp_ssl": False,
        "smtp_starttls": False,
        **extra,
    }


async def _timed_post(
    client: httpx.AsyncClient, url: str, json: dict[str, Any]
) -> tuple[httpx.Response, float]:
    started = time.monotonic()
    resp = await client.post(url, json=json, headers=HEADERS)
    return resp, time.monotonic() - started


def _assert_deadline_422(
    resp: httpx.Response,
    elapsed: float,
    *,
    code: str,
    stage: str,
    upper_bound: float,
) -> None:
    """The single behavioural assertion of ADR-0047 §1/§2.1/§3."""
    assert resp.status_code == 422, f"expected the domain 422, got {resp.status_code}: {resp.text}"
    err = resp.json()["error"]
    assert err["code"] == code
    assert err["details"]["detail"] == "timeout"
    assert err["details"]["stage"] == stage
    assert elapsed <= upper_bound, f"answered in {elapsed:.1f}s, budget was {upper_bound:.1f}s"


# ---------------------------------------------------------------------------
# §1/§2.1/§3 — the probe hangs → domain 422 within the deadline, with the stage
# ---------------------------------------------------------------------------


class TestDeadlineFiresWithStageAttribution:
    async def test_silent_imap_answers_422_imap_stage_within_deadline(
        self, client: httpx.AsyncClient, crm_db: AsyncEngine
    ) -> None:
        """Silent IMAP → ``imap_login_failed`` / ``stage=imap`` within the deadline."""
        async with silent_server() as imap, silent_server() as smtp:
            resp, elapsed = await _timed_post(
                client, "/api/external/mailboxes/test", _body(imap, smtp)
            )

        _assert_deadline_422(
            resp,
            elapsed,
            code="imap_login_failed",
            stage="imap",
            # IMAP probes live in a thread: teardown of the cancelled future is
            # ~0 (ADR-0047 §2.3), so the deadline itself is the whole budget.
            upper_bound=TEST_DEADLINE + SLACK,
        )

    async def test_silent_smtp_after_green_imap_answers_422_smtp_stage(
        self, client: httpx.AsyncClient, crm_db: AsyncEngine
    ) -> None:
        """IMAP green, SMTP silent → ``smtp_login_failed`` / ``stage=smtp``."""
        async with imap_server_ok() as imap, silent_server() as smtp:
            resp, elapsed = await _timed_post(
                client, "/api/external/mailboxes/test", _body(imap, smtp)
            )

        _assert_deadline_422(
            resp,
            elapsed,
            code="smtp_login_failed",
            stage="smtp",
            upper_bound=TEST_DEADLINE + _SMTP_QUIT_TIMEOUT + SLACK,
        )

    async def test_deadline_expiry_is_never_5xx(
        self, client: httpx.AsyncClient, crm_db: AsyncEngine
    ) -> None:
        """The failure mode the ADR exists to kill: a 500/504 instead of the 422."""
        async with silent_server() as imap, silent_server() as smtp:
            resp, _ = await _timed_post(client, "/api/external/mailboxes/test", _body(imap, smtp))

        assert resp.status_code < 500, f"deadline expiry surfaced as {resp.status_code}"
        assert resp.headers["content-type"].startswith("application/json")


# ---------------------------------------------------------------------------
# §2.3 — the teardown leg is bounded by _SMTP_QUIT_TIMEOUT, not _SMTP_TIMEOUT
# ---------------------------------------------------------------------------


class TestTeardownLeg:
    async def test_smtp_ignoring_quit_answers_within_deadline_plus_quit_timeout(
        self, client: httpx.AsyncClient, crm_db: AsyncEngine
    ) -> None:
        """§2.3: ``wait_for`` awaits the cancelled probe's ``finally`` (a polite QUIT).

        Against a server that answers neither the command nor the ``QUIT``, that
        leg MUST be bounded by ``_SMTP_QUIT_TIMEOUT`` (5 s) — with the bare
        ``client.quit()`` it would run to ``_SMTP_TIMEOUT`` (20 s) and the whole
        response would blow past nginx's 60 s into a ``504`` HTML.
        """
        seen: list[bytes] = []
        async with imap_server_ok() as imap, smtp_banner_then_silent(seen) as smtp:
            resp, elapsed = await _timed_post(
                client, "/api/external/mailboxes/test", _body(imap, smtp)
            )

        _assert_deadline_422(
            resp,
            elapsed,
            code="smtp_login_failed",
            stage="smtp",
            upper_bound=TEST_DEADLINE + _SMTP_QUIT_TIMEOUT + SLACK,
        )
        # The teardown leg really ran (the polite QUIT reached the server) and
        # was time-boxed — i.e. the bound above is not passing by accident.
        assert any(
            line.upper().startswith(b"QUIT") for line in seen
        ), f"the probe never sent QUIT — teardown leg not exercised; saw {seen!r}"
        assert (
            elapsed < TEST_DEADLINE + _SMTP_TIMEOUT
        ), "the QUIT ran to _SMTP_TIMEOUT instead of _SMTP_QUIT_TIMEOUT (ADR-0047 §2.3)"


# ---------------------------------------------------------------------------
# §1 — every entrance inherits the deadline (no side effects on expiry)
# ---------------------------------------------------------------------------


class TestAllEntrancesUnderTheDeadline:
    async def test_create_answers_422_and_does_not_persist_the_mailbox(
        self, client: httpx.AsyncClient, crm_db: AsyncEngine
    ) -> None:
        """``POST /mailboxes``: probe before INSERT → 422 in budget, nothing created."""
        async with silent_server() as imap, silent_server() as smtp:
            resp, elapsed = await _timed_post(
                client,
                "/api/external/mailboxes",
                _body(imap, smtp, display_name="never created"),
            )

        _assert_deadline_422(
            resp,
            elapsed,
            code="imap_login_failed",
            stage="imap",
            upper_bound=TEST_DEADLINE + SLACK,
        )
        async with crm_db.connect() as conn:
            count = await conn.scalar(
                text("SELECT count(*) FROM mail_accounts WHERE email = :e"),
                {"e": "probe@example.com"},
            )
        assert count == 0, "the mailbox was persisted despite the failed connection-test"

    async def test_patch_answers_422_and_does_not_change_credentials(
        self, client: httpx.AsyncClient, crm_db: AsyncEngine
    ) -> None:
        """``PATCH /mailboxes/{id}`` with new creds → 422 in budget, creds untouched."""
        account_id = await _seed_mailbox(crm_db, imap_host="imap.old.invalid")

        async with silent_server() as imap, silent_server() as smtp:
            started = time.monotonic()
            resp = await client.patch(
                f"/api/external/mailboxes/{account_id}",
                json={
                    "password": "brand-new-password",
                    "imap_host": imap[0],
                    "imap_port": imap[1],
                    "imap_ssl": False,
                    "smtp_host": smtp[0],
                    "smtp_port": smtp[1],
                    "smtp_ssl": False,
                    "smtp_starttls": False,
                },
                headers=HEADERS,
            )
            elapsed = time.monotonic() - started

        _assert_deadline_422(
            resp,
            elapsed,
            code="imap_login_failed",
            stage="imap",
            upper_bound=TEST_DEADLINE + SLACK,
        )
        from shared.crypto import decrypt_mail_password

        async with crm_db.connect() as conn:
            row = (
                await conn.execute(
                    text("SELECT imap_host, encrypted_password FROM mail_accounts WHERE id = :i"),
                    {"i": account_id},
                )
            ).one()
        assert row.imap_host == "imap.old.invalid", "host was changed despite the failed test"
        assert (
            decrypt_mail_password(row.encrypted_password, account_id) == _MAILBOX_PASSWORD
        ), "the stored password was overwritten despite the failed connection-test"

    async def test_reprobe_of_stored_mailbox_is_under_the_deadline(
        self, crm_db: AsyncEngine, deadline_settings: Any
    ) -> None:
        """``POST /mailboxes/test`` with ``account_id`` — the SECOND caller of P1.

        This is the branch ADR-0047 §1 calls out by name (``service.py:490``,
        ``_test_existing_account``): a call-site wrapper on ``test()`` would have
        left it unbounded. Driven at the service layer because the ``account_id``
        mode belongs to the INTERNAL schema (``MailAccountTestRequest``), which
        the external write API does not expose.
        """
        from backend.app.accounts.schemas import MailAccountTestRequest
        from backend.app.accounts.service import MailAccountService
        from backend.app.deps import VisibilityScope
        from backend.app.exceptions import IMAPLoginFailedError

        async with silent_server() as imap, silent_server() as smtp:
            account_id, owner_id = await _seed_mailbox(
                crm_db,
                imap_host=imap[0],
                imap_port=imap[1],
                smtp_host=smtp[0],
                smtp_port=smtp[1],
                with_owner=True,
            )
            factory = async_sessionmaker(bind=crm_db, expire_on_commit=False, class_=AsyncSession)
            scope = VisibilityScope(
                user_id=owner_id, role="super_admin", group_id=None, group_ids=frozenset()
            )
            async with factory() as session:
                service = MailAccountService(session)
                started = time.monotonic()
                with pytest.raises(IMAPLoginFailedError) as exc_info:
                    await service.test(MailAccountTestRequest(account_id=account_id), scope=scope)
                elapsed = time.monotonic() - started

        exc = exc_info.value
        assert exc.status_code == 422
        assert exc.code == "imap_login_failed"
        assert exc.details == {"detail": "timeout", "stage": "imap"}
        assert elapsed <= TEST_DEADLINE + SLACK, f"re-probe answered in {elapsed:.1f}s"


# ---------------------------------------------------------------------------
# §3 — oauth path (P2): a hung refresh exchange is attributed to ``oauth_token``
# ---------------------------------------------------------------------------


class TestOAuthProbeDeadline:
    async def test_hung_token_exchange_answers_imap_login_failed_oauth_token_stage(
        self, crm_db: AsyncEngine, deadline_settings: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """P2: the Microsoft token endpoint never answers → 422 ``oauth_token``.

        Only the EXTERNAL boundary is redirected: ``outlook_token_endpoint`` is
        pointed at a mock host that accepts the TCP connection and never replies.
        The real refresh code path runs; the deadline has to cut it.
        """
        from backend.app.accounts.schemas import MailAccountTestRequest
        from backend.app.accounts.service import MailAccountService
        from backend.app.deps import VisibilityScope
        from backend.app.exceptions import IMAPLoginFailedError
        from shared.config import Settings

        async with silent_server() as token_host:
            monkeypatch.setattr(
                Settings,
                "outlook_token_endpoint",
                f"http://{token_host[0]}:{token_host[1]}/token",
            )
            account_id, owner_id = await _seed_mailbox(crm_db, oauth=True, with_owner=True)
            factory = async_sessionmaker(bind=crm_db, expire_on_commit=False, class_=AsyncSession)
            scope = VisibilityScope(
                user_id=owner_id, role="super_admin", group_id=None, group_ids=frozenset()
            )
            async with factory() as session:
                service = MailAccountService(session)
                started = time.monotonic()
                with pytest.raises(IMAPLoginFailedError) as exc_info:
                    await service.test(MailAccountTestRequest(account_id=account_id), scope=scope)
                elapsed = time.monotonic() - started

        exc = exc_info.value
        assert exc.status_code == 422
        assert exc.code == "imap_login_failed"
        assert exc.details == {"detail": "timeout", "stage": "oauth_token"}
        assert elapsed <= TEST_DEADLINE + SLACK, f"oauth probe answered in {elapsed:.1f}s"


# ---------------------------------------------------------------------------
# §4 / TD-056 — the SSRF resolve runs OFF the event loop
# ---------------------------------------------------------------------------


class TestOffLoopResolve:
    async def test_hung_dns_does_not_block_the_loop_and_the_deadline_still_fires(
        self,
        client: httpx.AsyncClient,
        crm_db: AsyncEngine,
        deadline_settings: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """§4: ``getaddrinfo`` hangs → ``/healthz`` still answers, and the 422 lands.

        Before TD-056 the guard resolved IN the loop thread: a hung resolver
        stalled the whole container, and since ``asyncio.wait_for`` can only
        cancel at ``await`` points, the deadline was decorative. The concurrent
        ``/healthz`` is what tells the two apart — a blocked loop cannot answer.
        """
        from backend.app import security as sec_mod

        # The guard is a no-op outside prod, so the resolver would never be
        # reached in dev — switch APP_ENV for this test only.
        monkeypatch.setattr(deadline_settings, "APP_ENV", "prod")

        # A resolver that hangs for the probed MAIL hosts only (``*.example.com``)
        # and delegates every other name — ``socket.getaddrinfo`` is process-wide,
        # and the request itself resolves ``localhost`` for the rate-limiter's
        # redis before it ever reaches the guard. ``monkeypatch`` restores the
        # real resolver at teardown, so nothing leaks into the next test.
        monkeypatch.setattr(
            sec_mod.socket,
            "getaddrinfo",
            # Outlasts the deadline (and its slack) by a wide margin; kept short
            # because the abandoned worker thread is joined at loop shutdown.
            hung_getaddrinfo(TEST_DEADLINE + SLACK + 2.0),
        )

        async def _probe() -> tuple[httpx.Response, float]:
            return await _timed_post(
                client,
                "/api/external/mailboxes/test",
                _body(("imap.example.com", 993), ("smtp.example.com", 587)),
            )

        probe = asyncio.create_task(_probe())
        # Give the request a moment to reach the (now hanging) resolver.
        await asyncio.sleep(0.5)

        health_started = time.monotonic()
        health = await client.get("/healthz")
        health_elapsed = time.monotonic() - health_started

        assert health.status_code == 200
        assert health_elapsed < 1.0, (
            f"/healthz took {health_elapsed:.1f}s while DNS hung — the event loop is BLOCKED, "
            "so the resolve is not off-loop (ADR-0047 §4 / TD-056)"
        )
        assert not probe.done(), "the probe should still be hanging on the resolver"

        resp, elapsed = await probe
        _assert_deadline_422(
            resp,
            elapsed,
            code="imap_login_failed",
            stage="imap",
            upper_bound=TEST_DEADLINE + SLACK,
        )


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


async def _seed_mailbox(
    engine: AsyncEngine,
    *,
    imap_host: str = "imap.example.com",
    imap_port: int = 993,
    smtp_host: str = "smtp.example.com",
    smtp_port: int = 587,
    oauth: bool = False,
    with_owner: bool = False,
) -> Any:
    """Insert one mailbox owned by ``crm-service``; return its id (and owner id).

    The id is pre-allocated from the sequence because the credential blobs are
    AEAD-bound to it (AAD = ``mail_account_id``) and the table's CHECK
    constraints demand the blob in the very same INSERT.
    """
    from shared.crypto import encrypt_mail_password

    factory = async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as session, session.begin():
        owner_id = await session.scalar(text("SELECT id FROM users WHERE username = 'crm-service'"))
        account_id: int = await session.scalar(
            text("SELECT nextval(pg_get_serial_sequence('mail_accounts', 'id'))")
        )
        blob = encrypt_mail_password(_MAILBOX_PASSWORD, account_id)
        await session.execute(
            text(
                """
                INSERT INTO mail_accounts (
                    id, user_id, email, auth_type, oauth_provider, oauth_needs_consent,
                    encrypted_password, oauth_refresh_token_encrypted,
                    imap_host, imap_port, imap_ssl,
                    smtp_host, smtp_port, smtp_ssl, smtp_starttls, is_active
                ) VALUES (
                    :id, :uid, :email, :auth_type, :provider, false,
                    :pwd, :refresh,
                    :ih, :ip, false,
                    :sh, :sp, false, false, true
                )
                """
            ),
            {
                "id": account_id,
                "uid": owner_id,
                "email": "stored@example.com",
                "auth_type": "oauth_outlook" if oauth else "password",
                "provider": "outlook" if oauth else None,
                "pwd": None if oauth else blob,
                "refresh": blob if oauth else None,
                "ih": imap_host,
                "ip": imap_port,
                "sh": smtp_host,
                "sp": smtp_port,
            },
        )
    return (account_id, owner_id) if with_owner else account_id
