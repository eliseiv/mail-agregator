"""Integration tests for ``CrmPushService`` / ``CrmStatusService`` (ADR-0043 §2).

Against a real Postgres (``db_session`` fixture) with ``_post_signed`` monkeypatched (no
network). Cover: ``mark_pushed`` guarded (``WHERE pushed_at IS NULL``, idempotent), 2xx ->
``pushed_at`` stamped, non-2xx / transport error -> ``ok=False`` and ``pushed_at`` NOT
stamped (recovery picks it up), recovery candidates are ``pushed_at IS NULL`` within the
window only, idempotency of a repeated 2xx (``marked=0``), status channel 2xx / non-2xx /
missing account.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.crm_push import service as svc
from backend.app.crm_push.service import CrmPushService, CrmStatusService, PushResult
from backend.app.repositories.mail_accounts import MailAccountsRepo
from backend.app.repositories.messages import MessagesRepo
from shared.crypto import encrypt_mail_password
from shared.models import MailAccount, Message, User

pytestmark = pytest.mark.integration  # needs DB


async def _seed_account(session: AsyncSession, *, is_active: bool = True) -> int:
    user = User(username=f"crmpush_{datetime.now(UTC).timestamp()}")
    session.add(user)
    await session.flush()
    # id is needed to key the credential encryption (ck_mail_accounts_password_creds
    # requires an encrypted_password for password auth) — allocate it up front.
    account_id = await MailAccountsRepo(session).next_account_id()
    acc = MailAccount(
        id=account_id,
        user_id=user.id,
        email="box@example.com",
        encrypted_password=encrypt_mail_password("p", account_id),
        imap_host="imap.example.com",
        smtp_host="smtp.example.com",
        is_active=is_active,
        consecutive_failures=0,
    )
    session.add(acc)
    await session.flush()
    return int(acc.id)


async def _seed_message(
    session: AsyncSession,
    *,
    account_id: int,
    uid: int,
    pushed_at: datetime | None = None,
    fetched_at: datetime | None = None,
) -> int:
    msg = Message(
        mail_account_id=account_id,
        uid=uid,
        uidvalidity=1,
        from_addr="s@e.com",
        internal_date=datetime(2026, 7, 1, tzinfo=UTC),
        body_text="body",
        pushed_at=pushed_at,
        fetched_at=fetched_at or datetime.now(UTC),
    )
    session.add(msg)
    await session.flush()
    return int(msg.id)


def _fake_post(status_code: int) -> Callable[[str, dict[str, object]], Awaitable[httpx.Response]]:
    async def _post(url: str, body: dict[str, object]) -> httpx.Response:
        return httpx.Response(
            status_code,
            text="ok" if status_code < 300 else "err",
            request=httpx.Request("POST", url),
        )

    return _post


def _raise_post(exc: Exception) -> Callable[[str, dict[str, object]], Awaitable[httpx.Response]]:
    async def _post(url: str, body: dict[str, object]) -> httpx.Response:
        raise exc

    return _post


async def _pushed_at(session: AsyncSession, message_id: int) -> datetime | None:
    return (
        await session.execute(select(Message.pushed_at).where(Message.id == message_id))
    ).scalar_one()


# --------------------------------------------------------- mark_pushed guarded
async def test_mark_pushed_only_updates_null_rows(db_session: AsyncSession) -> None:
    acc = await _seed_account(db_session)
    already = datetime(2026, 6, 1, tzinfo=UTC)
    m_null = await _seed_message(db_session, account_id=acc, uid=1)
    m_set = await _seed_message(db_session, account_id=acc, uid=2, pushed_at=already)

    marked = await MessagesRepo(db_session).mark_pushed([m_null, m_set])
    assert marked == 1  # only the NULL row transitions
    assert await _pushed_at(db_session, m_null) is not None
    # an already-stamped row does not move (idempotent)
    assert await _pushed_at(db_session, m_set) == already


# ------------------------------------------------------ push_message_ids: 2xx
async def test_push_2xx_stamps_pushed_at(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    acc = await _seed_account(db_session)
    m1 = await _seed_message(db_session, account_id=acc, uid=1)
    m2 = await _seed_message(db_session, account_id=acc, uid=2)
    monkeypatch.setattr(svc, "_post_signed", _fake_post(200))

    result = await CrmPushService(db_session).push_message_ids([m1, m2])
    assert result == PushResult(ok=True, delivered=2, marked=2, missing=0)
    assert await _pushed_at(db_session, m1) is not None
    assert await _pushed_at(db_session, m2) is not None


# -------------------------------------------------- push_message_ids: non-2xx
async def test_push_non_2xx_leaves_unmarked(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    acc = await _seed_account(db_session)
    m1 = await _seed_message(db_session, account_id=acc, uid=1)
    monkeypatch.setattr(svc, "_post_signed", _fake_post(500))

    result = await CrmPushService(db_session).push_message_ids([m1])
    assert result.ok is False
    assert await _pushed_at(db_session, m1) is None  # unmarked -> recovery picks it up


async def test_push_transport_error_leaves_unmarked(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    acc = await _seed_account(db_session)
    m1 = await _seed_message(db_session, account_id=acc, uid=1)
    monkeypatch.setattr(svc, "_post_signed", _raise_post(httpx.ConnectError("boom")))

    result = await CrmPushService(db_session).push_message_ids([m1])
    assert result.ok is False
    assert await _pushed_at(db_session, m1) is None


# ------------------------------------------------ idempotency of a repeated 2xx
async def test_second_2xx_marks_zero_and_recovery_drops(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    acc = await _seed_account(db_session)
    m1 = await _seed_message(db_session, account_id=acc, uid=1)
    monkeypatch.setattr(svc, "_post_signed", _fake_post(200))
    service = CrmPushService(db_session)

    first = await service.push_message_ids([m1])
    assert first.marked == 1
    # a repeated 2xx (e.g. a CRM-side duplicate): pushed_at already set -> marked=0.
    second = await service.push_message_ids([m1])
    assert second.ok is True and second.marked == 0
    # recovery no longer picks it up (no endless re-push).
    pending = await service.list_recovery_candidates(window_hours=720, limit=100)
    assert m1 not in pending


# --------------------------------------------------- recovery candidates
async def test_recovery_candidates_pending_only_in_window(db_session: AsyncSession) -> None:
    acc = await _seed_account(db_session)
    pending = await _seed_message(db_session, account_id=acc, uid=1)
    pushed = await _seed_message(
        db_session, account_id=acc, uid=2, pushed_at=datetime(2026, 6, 1, tzinfo=UTC)
    )
    stale = await _seed_message(
        db_session, account_id=acc, uid=3, fetched_at=datetime.now(UTC) - timedelta(hours=48)
    )
    candidates = await CrmPushService(db_session).list_recovery_candidates(
        window_hours=24, limit=100
    )
    assert pending in candidates
    assert pushed not in candidates  # already delivered
    assert stale not in candidates  # out of window (fetched_at older than 24h)


async def test_push_missing_ids_no_error(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(svc, "_post_signed", _fake_post(200))
    result = await CrmPushService(db_session).push_message_ids([999_999])
    assert result.ok is True and result.delivered == 0 and result.missing == 1


# ============================================================ status channel
async def test_status_2xx_returns_true(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    acc = await _seed_account(db_session, is_active=False)
    monkeypatch.setattr(svc, "_post_signed", _fake_post(200))
    assert await CrmStatusService(db_session).push_status(acc) is True


async def test_status_non_2xx_returns_false(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    acc = await _seed_account(db_session)
    monkeypatch.setattr(svc, "_post_signed", _fake_post(503))
    assert await CrmStatusService(db_session).push_status(acc) is False


async def test_status_missing_account_returns_true(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(svc, "_post_signed", _fake_post(200))
    # a missing account has nothing to deliver — not an error (no endless re-enqueue).
    assert await CrmStatusService(db_session).push_status(888_888) is True
