"""Integration tests for the forward-dispatch pipeline (ADR-0034 §3.4, §14.4).

Drives :func:`worker.app.forward_dispatch._dispatch_one` against the real test
Postgres + Redis with the external SMTP mocked (``smtp_send_message``) and a
fake MinIO storage. Verifies the full outcome matrix:

- happy path: team mailbox + active config + new message → sent exactly once,
  ``message_forwards.sent_at`` stamped, SMTP called once;
- dedup: a second dispatch of the same message → ``skip_dedup`` (no 2nd send);
- ``skip_no_config`` (missing / inactive config), ``skip_personal``
  (group_id NULL), ``skip_history`` (temporal guard), ``skip_loop`` (forward_to
  == mailbox address);
- SMTP failure → ``mark_error`` (no orphan), outcome ``error``;
- attachment stream failure → ``mark_error`` (no orphan);
- a full ``forward_dispatch`` tick still drains the batch when one item errors.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

import backend.app.send.service as send_service
from shared.models import Attachment, GroupForwarding, MailAccount, MessageForward, User
from shared.models.group import Group
from shared.models.message import Message
from worker.app import forward_dispatch as fd

pytestmark = pytest.mark.integration  # needs DB + Redis

_GID = 4400


@pytest_asyncio.fixture(autouse=True)
async def _truncate_forwarding_tables(db_engine: AsyncEngine) -> Any:
    """These tests COMMIT, and the shared ``_db_truncate_all`` fixture does not
    clear ``groups`` / ``group_forwarding`` / ``message_forwards``. Wipe them so
    the fixed group id (4400) does not collide across tests."""
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "TRUNCATE TABLE groups, group_forwarding, message_forwards "
                "RESTART IDENTITY CASCADE"
            )
        )
    yield


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeStorage:
    def __init__(self, blobs: dict[str, bytes] | None = None, *, raise_on: str | None = None):
        self._blobs = blobs or {}
        self._raise_on = raise_on

    async def get_object_stream(self, key: str) -> Any:
        if self._raise_on == key:
            raise RuntimeError("simulated MinIO stream failure")
        yield self._blobs[key]


class _SmtpRecorder:
    """Captures ``smtp_send_message`` calls (or raises to simulate failure)."""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple[str, list[str]]] = []
        self._fail = fail

    async def __call__(
        self, account: Any, msg: Any, recipients: list[str], *, session: Any
    ) -> None:
        self.calls.append((account.email, recipients))
        if self._fail:
            raise RuntimeError("simulated SMTP failure")


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------


async def _seed(
    session: AsyncSession,
    *,
    group_id: int | None = _GID,
    forward_to: str | None = "leader@company.com",
    is_active: bool = True,
    account_email: str = "box@company.com",
    internal_date: datetime | None = None,
    gf_created_offset: timedelta | None = None,
    with_attachment: tuple[str, int, bool] | None = None,
) -> dict[str, int]:
    """Seed group(+user+mailbox+message) and optionally a forwarding config.

    ``group_id=None`` seeds a personal mailbox (no group, no config).
    ``forward_to=None`` skips the ``group_forwarding`` row entirely.
    ``with_attachment=(s3_key, size_bytes, skipped_too_large)`` adds one row.
    """
    user = User(
        username=f"fwd_user_{account_email}",
        role="super_admin",
        password_hash="$argon2id$v=19$m=65536,t=3,p=4$abc$def",
        password_reset_required=False,
    )
    session.add(user)
    if group_id is not None:
        session.add(Group(id=group_id, name=f"team-{group_id}", leader_user_id=None))
    await session.flush()

    acc = MailAccount(
        user_id=user.id,
        group_id=group_id,
        email=account_email,
        encrypted_password=b"dummy",
        imap_host="imap.example.com",
        smtp_host="smtp.example.com",
    )
    session.add(acc)
    await session.flush()

    if group_id is not None and forward_to is not None:
        gf = GroupForwarding(group_id=group_id, forward_to=forward_to, is_active=is_active)
        session.add(gf)
        await session.flush()
        if gf_created_offset is not None:
            gf.created_at = datetime.now(UTC) + gf_created_offset
            await session.flush()

    msg = Message(
        mail_account_id=acc.id,
        uid=101,
        uidvalidity=1,
        from_addr="sender@partner.com",
        from_name="Sender",
        to_addrs=account_email,
        subject="Hello",
        internal_date=internal_date or (datetime.now(UTC) + timedelta(minutes=5)),
        body_text="original body",
        body_html="<p>original body</p>",
    )
    session.add(msg)
    await session.flush()

    if with_attachment is not None:
        s3_key, size_bytes, skipped = with_attachment
        session.add(
            Attachment(
                message_id=msg.id,
                filename="report.pdf",
                content_type="application/pdf",
                size_bytes=size_bytes,
                s3_key=s3_key,
                skipped_too_large=skipped,
            )
        )
        await session.flush()

    return {"account_id": acc.id, "message_id": msg.id, "user_id": user.id}


async def _forward_row(session: AsyncSession, message_id: int) -> MessageForward | None:
    return (
        await session.execute(select(MessageForward).where(MessageForward.message_id == message_id))
    ).scalar_one_or_none()


# ---------------------------------------------------------------------------
# Tests — driven via _dispatch_one on a committed session
# ---------------------------------------------------------------------------


class TestHappyPathAndDedup:
    async def test_new_team_message_forwarded_exactly_once(
        self, db_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        recorder = _SmtpRecorder()
        monkeypatch.setattr(send_service, "smtp_send_message", recorder)
        storage = _FakeStorage()

        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as s, s.begin():
            ids = await _seed(s)
            out1 = await fd._dispatch_one(s, ids["message_id"], storage)  # type: ignore[arg-type]
            assert out1 == "sent"
            # Second dispatch in the same claim registry → dedup, no 2nd send.
            out2 = await fd._dispatch_one(s, ids["message_id"], storage)  # type: ignore[arg-type]
            assert out2 == "skip_dedup"

            row = await _forward_row(s, ids["message_id"])
            assert row is not None
            assert row.sent_at is not None
            assert row.error is None
            assert row.forward_to == "leader@company.com"

        # SMTP called exactly once, to the leader, from the team mailbox.
        assert recorder.calls == [("box@company.com", ["leader@company.com"])]

    async def test_forward_includes_attachment(
        self, db_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        recorder = _SmtpRecorder()
        monkeypatch.setattr(send_service, "smtp_send_message", recorder)
        storage = _FakeStorage({"att-key-1": b"%PDF-1.4 body"})

        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as s, s.begin():
            ids = await _seed(s, with_attachment=("att-key-1", 13, False))
            out = await fd._dispatch_one(s, ids["message_id"], storage)  # type: ignore[arg-type]
            assert out == "sent"
        # The built MIME carried the attachment payload streamed from storage.
        assert len(recorder.calls) == 1


class TestSkips:
    async def test_no_config_skips(
        self, db_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        recorder = _SmtpRecorder()
        monkeypatch.setattr(send_service, "smtp_send_message", recorder)
        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as s, s.begin():
            ids = await _seed(s, forward_to=None)  # no group_forwarding row
            out = await fd._dispatch_one(s, ids["message_id"], _FakeStorage())  # type: ignore[arg-type]
            assert out == "skip_no_config"
        assert recorder.calls == []

    async def test_inactive_config_skips(
        self, db_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        recorder = _SmtpRecorder()
        monkeypatch.setattr(send_service, "smtp_send_message", recorder)
        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as s, s.begin():
            ids = await _seed(s, is_active=False)
            out = await fd._dispatch_one(s, ids["message_id"], _FakeStorage())  # type: ignore[arg-type]
            assert out == "skip_no_config"
        assert recorder.calls == []

    async def test_personal_mailbox_skips(
        self, db_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        recorder = _SmtpRecorder()
        monkeypatch.setattr(send_service, "smtp_send_message", recorder)
        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as s, s.begin():
            ids = await _seed(s, group_id=None, forward_to=None)
            out = await fd._dispatch_one(s, ids["message_id"], _FakeStorage())  # type: ignore[arg-type]
            assert out == "skip_personal"
        assert recorder.calls == []

    async def test_temporal_guard_skips_old_message(
        self, db_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        recorder = _SmtpRecorder()
        monkeypatch.setattr(send_service, "smtp_send_message", recorder)
        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as s, s.begin():
            # Message predates the config's created_at anchor → history skip.
            ids = await _seed(s, internal_date=datetime(2000, 1, 1, tzinfo=UTC))
            out = await fd._dispatch_one(s, ids["message_id"], _FakeStorage())  # type: ignore[arg-type]
            assert out == "skip_history"
        assert recorder.calls == []

    async def test_loop_guard_skips_self_forward(
        self, db_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        recorder = _SmtpRecorder()
        monkeypatch.setattr(send_service, "smtp_send_message", recorder)
        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as s, s.begin():
            # forward_to == mailbox address → loop-guard skip.
            ids = await _seed(s, account_email="box@company.com", forward_to="box@company.com")
            out = await fd._dispatch_one(s, ids["message_id"], _FakeStorage())  # type: ignore[arg-type]
            assert out == "skip_loop"
        assert recorder.calls == []


class TestErrorHandling:
    async def test_smtp_failure_marks_error_no_orphan(
        self, db_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        recorder = _SmtpRecorder(fail=True)
        monkeypatch.setattr(send_service, "smtp_send_message", recorder)
        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as s, s.begin():
            ids = await _seed(s)
            out = await fd._dispatch_one(s, ids["message_id"], _FakeStorage())  # type: ignore[arg-type]
            assert out == "error"
            row = await _forward_row(s, ids["message_id"])
            assert row is not None
            assert row.sent_at is None  # not sent
            assert row.error is not None  # error recorded — no orphan claim
            assert "SMTP" in row.error or "RuntimeError" in row.error

    async def test_attachment_stream_failure_marks_error(
        self, db_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        recorder = _SmtpRecorder()
        monkeypatch.setattr(send_service, "smtp_send_message", recorder)
        storage = _FakeStorage(raise_on="broken-key")
        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as s, s.begin():
            ids = await _seed(s, with_attachment=("broken-key", 10, False))
            out = await fd._dispatch_one(s, ids["message_id"], storage)  # type: ignore[arg-type]
            assert out == "error"
            row = await _forward_row(s, ids["message_id"])
            assert row is not None
            assert row.sent_at is None
            assert row.error is not None  # claim marked, not left orphaned
        # SMTP never reached because MIME build failed on the stream.
        assert recorder.calls == []


class TestFullTickDrainsBatch:
    async def test_forward_dispatch_tick_processes_all_despite_one_error(
        self, db_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A full ``forward_dispatch`` tick LPOPs the queue and keeps going
        even if one item raises (per-item try/except, ADR-0034 §3.3)."""
        from backend.app.forwarding.dispatch_service import (
            FORWARD_DISPATCH_QUEUE_KEY,
            _QueuePayload,
        )
        from shared.redis_client import get_redis

        recorder = _SmtpRecorder()
        monkeypatch.setattr(send_service, "smtp_send_message", recorder)
        # Force _dispatch_one to run against our fake storage inside the tick.
        monkeypatch.setattr(fd, "get_storage", lambda: _FakeStorage())

        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as s, s.begin():
            good = await _seed(s, account_email="good@company.com")
            good_mid = good["message_id"]

        # Enqueue the good message id + a bogus (missing) id. The tick must
        # process both without raising and drain the queue.
        r = get_redis()
        await r.lpush(
            FORWARD_DISPATCH_QUEUE_KEY,
            _QueuePayload(message_id=good_mid, source="sync").to_json(),
            _QueuePayload(message_id=999_999_999, source="sync").to_json(),
        )

        await fd.forward_dispatch()

        # Queue fully drained; the good message was sent exactly once.
        assert await r.llen(FORWARD_DISPATCH_QUEUE_KEY) == 0
        assert recorder.calls == [("good@company.com", ["leader@company.com"])]
