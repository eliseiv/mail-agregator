"""Fixtures for the external PULL-API integration tests (ADR-0029).

The router reads ``get_settings().external_api_enabled`` / ``EXTERNAL_API_KEY``
at request time, so flipping the feature on/off for a test means setting the
env var and clearing the ``lru_cache`` on :func:`shared.config.get_settings`
(mirrors the ``set_tg_notify_all`` pattern in the telegram package conftest).

Seeding helpers build ``users`` / ``mail_accounts`` / ``messages`` directly via the
DB so the keyset / canonical-dedup paths are exercised against real Postgres — never
a mock of our own code (only the API boundary uses HTTP).

ADR-0044 §4 (phase A1/A3): tags, teams/groups and the session super-admin seed are
decommissioned. The tag/group seeding helpers went with them, and the mailbox OWNER
is now the technical ``crm-service`` user (the only user the app seeds — ADR-0039)
— hence ``owner`` below replaces the old ``super_admin`` fixture.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from shared.config import get_settings
from shared.crypto import encrypt_mail_password
from shared.models import MailAccount, Message, User

# A deterministic 256-bit-ish test key (the contract only needs a constant-time
# match; value is arbitrary). Never a real secret.
TEST_API_KEY = "test_external_api_key_deadbeefdeadbeefdeadbeefdeadbeef"


@pytest.fixture
def set_external_api_key(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[str], None]]:
    """Set ``EXTERNAL_API_KEY`` and reload the lru-cached settings.

    ``_set("")`` turns the feature OFF (endpoint then 401s unenumerably);
    ``_set(TEST_API_KEY)`` turns it ON. The cache is cleared again on teardown
    so later tests observe the real env value.
    """

    def _set(value: str) -> None:
        monkeypatch.setenv("EXTERNAL_API_KEY", value)
        get_settings.cache_clear()
        reloaded = get_settings()
        assert value == reloaded.EXTERNAL_API_KEY
        assert reloaded.external_api_enabled is bool(value)

    yield _set
    get_settings.cache_clear()


@pytest_asyncio.fixture
async def api_key_on(
    set_external_api_key: Callable[[str], None],
) -> str:
    """Turn the feature ON for the test and return the active key."""
    set_external_api_key(TEST_API_KEY)
    return TEST_API_KEY


@pytest.fixture
def set_external_rate_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Callable[[int], None]]:
    """Override ``EXTERNAL_API_RATE_LIMIT_PER_MINUTE`` and reload settings.

    The router builds the runtime :class:`Limit` from
    ``settings.EXTERNAL_API_RATE_LIMIT_PER_MINUTE`` at consume-time (ADR-0029
    §4 — same override pattern as ``WEBHOOK_TEST_LIMIT``), reading
    ``get_settings()`` fresh on every request. So setting the env var and
    clearing the ``lru_cache`` here makes the very next request observe the new
    cap. Mirrors :func:`set_external_api_key`. Cache cleared again on teardown.
    """

    def _set(value: int) -> None:
        monkeypatch.setenv("EXTERNAL_API_RATE_LIMIT_PER_MINUTE", str(value))
        get_settings.cache_clear()
        reloaded = get_settings()
        assert value == reloaded.EXTERNAL_API_RATE_LIMIT_PER_MINUTE

    yield _set
    get_settings.cache_clear()


@pytest.fixture
def set_external_write_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Callable[[bool], None]]:
    """Flip ``EXTERNAL_WRITE_ENABLED`` and reload the lru-cached settings.

    The write router reads ``settings.EXTERNAL_WRITE_ENABLED`` fresh on every
    request (the write-gate, step 5), so setting the env var + clearing the
    cache makes the very next request observe the change. Cache cleared again on
    teardown (mirrors :func:`set_external_api_key`).
    """

    def _set(value: bool) -> None:
        monkeypatch.setenv("EXTERNAL_WRITE_ENABLED", "true" if value else "false")
        get_settings.cache_clear()
        reloaded = get_settings()
        assert reloaded.EXTERNAL_WRITE_ENABLED is value

    yield _set
    get_settings.cache_clear()


@pytest.fixture
def set_external_write_rate_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Callable[[int], None]]:
    """Override ``EXTERNAL_WRITE_RATE_LIMIT_PER_MINUTE`` and reload settings.

    The write router builds the runtime :class:`Limit` from
    ``settings.EXTERNAL_WRITE_RATE_LIMIT_PER_MINUTE`` at consume-time, so a low
    value here lets a test prove the 429 fires FIRST (before auth/gate/body).
    """

    def _set(value: int) -> None:
        monkeypatch.setenv("EXTERNAL_WRITE_RATE_LIMIT_PER_MINUTE", str(value))
        get_settings.cache_clear()
        reloaded = get_settings()
        assert value == reloaded.EXTERNAL_WRITE_RATE_LIMIT_PER_MINUTE

    yield _set
    get_settings.cache_clear()


@pytest_asyncio.fixture
async def write_api_on(
    set_external_api_key: Callable[[str], None],
    set_external_write_enabled: Callable[[bool], None],
) -> str:
    """Turn the whole external WRITE surface ON: valid key + write-gate enabled.

    Order matters — ``set_external_write_enabled`` clears the settings cache
    AFTER the key is set, so both env vars are live for the next request.
    """
    set_external_api_key(TEST_API_KEY)
    set_external_write_enabled(True)
    return TEST_API_KEY


async def _get_owner(db_engine: AsyncEngine) -> User:
    """The ``crm-service`` technical user — the owner of every mailbox (ADR-0039).

    ADR-0044 §5: ``seed_super_admin`` went away with the cookie UI, so ``crm-service``
    is the only user the app lifespan seeds.
    """
    from backend.app.auth.service import CRM_SERVICE_USERNAME

    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with factory() as ses:
        u = (
            await ses.execute(select(User).where(User.username == CRM_SERVICE_USERNAME))
        ).scalar_one_or_none()
        assert u is not None, "crm-service must be seeded by app startup"
        return u


async def _make_mail_account(
    db_engine: AsyncEngine,
    *,
    user_id: int,
    email: str,
    display_name: str | None = None,
) -> MailAccount:
    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with factory() as ses, ses.begin():
        from backend.app.repositories.mail_accounts import MailAccountsRepo

        new_id = await MailAccountsRepo(ses).next_account_id()
        blob = encrypt_mail_password("p", new_id)
        acc = MailAccount(
            id=new_id,
            user_id=user_id,
            email=email,
            display_name=display_name,
            encrypted_password=blob,
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
        await ses.refresh(acc)
        return acc


async def _make_message(
    db_engine: AsyncEngine,
    *,
    mail_account_id: int,
    uid: int,
    subject: str | None = "Hello",
    from_addr: str = "sender@x.com",
    from_name: str | None = "Sender Name",
    to_addrs: str = "me@example.com",
    cc_addrs: str | None = None,
    internal_date: datetime | None = None,
    body_text: str = "body",
    body_html: str | None = None,
    body_present: bool = True,
    body_truncated: bool = False,
) -> Message:
    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with factory() as ses, ses.begin():
        m = Message(
            mail_account_id=mail_account_id,
            uid=uid,
            uidvalidity=1,
            from_addr=from_addr,
            from_name=from_name,
            to_addrs=to_addrs,
            cc_addrs=cc_addrs,
            subject=subject,
            internal_date=internal_date or datetime.now(UTC),
            body_text=body_text,
            body_html=body_html,
            body_present=body_present,
            body_truncated=body_truncated,
        )
        ses.add(m)
        await ses.flush()
        await ses.refresh(m)
        return m


async def _make_secondary_owner_mailbox(
    db_engine: AsyncEngine,
    *,
    username: str,
    email: str,
    display_name: str | None = None,
) -> MailAccount:
    """Create a SECOND user + a mailbox of theirs in ONE transaction.

    Feeds the canonical-dedup cases (two mailboxes with the same ``LOWER(email)``
    under different owners → the external API returns only ``MIN(id)``), which is
    still live after the decommission. ADR-0044 §1: there are no groups any more, so
    the "second team" of the old helper is just a second owner row.

    One ``ses.begin()`` (instead of several short-lived sessions) avoids lock
    contention with the app-lifespan seed / autouse TRUNCATE — the test must be
    deterministic, not flaky.
    """
    from backend.app.repositories.mail_accounts import MailAccountsRepo

    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with factory() as ses, ses.begin():
        u = User(username=username, display_name=display_name or username, role="super_admin")
        ses.add(u)
        await ses.flush()

        new_id = await MailAccountsRepo(ses).next_account_id()
        acc = MailAccount(
            id=new_id,
            user_id=u.id,
            email=email,
            display_name=display_name,
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
        await ses.refresh(acc)
        return acc


@pytest_asyncio.fixture
def make_secondary_owner_mailbox(db_engine: AsyncEngine) -> Callable[..., Any]:
    async def _create(*, username: str, email: str, display_name: str | None = None) -> MailAccount:
        return await _make_secondary_owner_mailbox(
            db_engine, username=username, email=email, display_name=display_name
        )

    return _create


@pytest_asyncio.fixture
async def owner(db_engine: AsyncEngine) -> User:
    """Owner of the seeded mailboxes = the ``crm-service`` technical user."""
    return await _get_owner(db_engine)


@pytest_asyncio.fixture
async def crm_service_user(db_engine: AsyncEngine) -> User:
    """The ``crm-service`` technical user seeded at app startup (ADR-0039)."""
    from backend.app.auth.service import CRM_SERVICE_USERNAME

    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with factory() as ses:
        u = (
            await ses.execute(select(User).where(User.username == CRM_SERVICE_USERNAME))
        ).scalar_one_or_none()
        assert u is not None, "crm-service must be seeded by app startup (seed_crm_service_user)"
        return u


@pytest.fixture
def patch_mail_testers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the external IMAP/SMTP connectivity probe pass without a live server.

    The mail servers are the EXTERNAL boundary (mocking our own code is
    forbidden, mocking third-party services is allowed): the create flow calls
    ``MailAccountService.test`` → ``imap_test_login`` / ``smtp_test_login`` which
    open real sockets. We stub exactly those two boundary functions to no-op so
    ``create`` reaches the persistence + owner-assignment logic under test.
    """

    async def _ok(**_kw: Any) -> None:
        return None

    monkeypatch.setattr("backend.app.accounts.service.imap_test_login", _ok)
    monkeypatch.setattr("backend.app.accounts.service.smtp_test_login", _ok)


@pytest_asyncio.fixture
def make_mail_account(db_engine: AsyncEngine) -> Callable[..., Any]:
    async def _create(
        user_id: int,
        email: str,
        *,
        display_name: str | None = None,
    ) -> MailAccount:
        return await _make_mail_account(
            db_engine,
            user_id=user_id,
            email=email,
            display_name=display_name,
        )

    return _create


@pytest_asyncio.fixture
def make_message(db_engine: AsyncEngine) -> Callable[..., Any]:
    async def _create(mail_account_id: int, uid: int, **kw: Any) -> Message:
        return await _make_message(db_engine, mail_account_id=mail_account_id, uid=uid, **kw)

    return _create


@pytest_asyncio.fixture
def seed_n_messages(
    db_engine: AsyncEngine,
    owner: User,
    make_mail_account: Callable[..., Any],
    make_message: Callable[..., Any],
) -> Callable[..., Any]:
    """Seed ``n`` messages on one mailbox; return the ordered ``message_ids``.

    ``internal_date`` is set DESCENDING with uid so a naive ``ORDER BY
    internal_date`` would reverse the rows — proving the external keyset is
    over ``id ASC`` (not date) — see ADR-0029 §1.
    """

    async def _seed(n: int, *, email: str = "seed@example.com") -> list[int]:
        acc = await make_mail_account(owner.id, email)
        base = datetime.now(UTC)
        ids: list[int] = []
        for i in range(n):
            m = await make_message(
                acc.id,
                uid=1000 + i,
                subject=f"subj-{i}",
                internal_date=base - timedelta(minutes=i),
                body_text=f"body-{i}",
            )
            ids.append(m.id)
        return ids

    return _seed
