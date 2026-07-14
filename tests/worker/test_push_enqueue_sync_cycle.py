"""ADR-0043 §2 — ``sync_cycle`` enqueues every inserted message onto ``crm_push_queue``.

Source of truth: ``worker/app/sync_cycle.py`` (the ``if crm_push_ids and
settings.crm_push_enabled:`` block after the ``mark_sync_success`` commit) +
``backend/app/crm_push/service.py`` (``enqueue_push_ids`` / ``CRM_PUSH_QUEUE_KEY``).

ADR-0044 §4 (phase A3): the Telegram ``push_notify_queue`` (ADR-0027 team bots),
the ``tg_notify`` / ``webhook`` / ``forward`` enqueues and the tag-apply hook were
all removed from the cycle. The ONLY message fan-out left is the CRM push-outbox
— which is what this suite now pins:

- gate ON  → the inserted message id lands on ``crm_push_queue`` with ``source="sync"``;
- gate OFF (no ``CRM_INGEST_URL`` / ``CRM_PUSH_SECRET``) → the queue stays empty;
- a Redis outage on the LPUSH is swallowed — the message is still persisted and the
  account sync still reports ``ok`` (independent try/except, ADR-0043 §2).

We drive ``sync_one_account`` with a mocked IMAP fetch (one message) and assert on the
REAL Redis queue — nothing of our own code is mocked besides the IMAP boundary.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from backend.app.crm_push.service import CRM_PUSH_QUEUE_KEY
from shared.config import get_settings
from shared.crypto import encrypt_mail_password
from shared.models import MailAccount, User
from worker.app import sync_cycle as sc

pytestmark = pytest.mark.integration  # needs DB + Redis


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def enable_crm_push(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Turn the CRM push gate ON (``crm_push_enabled`` = URL + secret)."""
    monkeypatch.setenv("CRM_INGEST_URL", "https://crm.example.com/api/mail/ingest")
    monkeypatch.setenv("CRM_PUSH_SECRET", "test_push_secret")
    get_settings.cache_clear()
    assert get_settings().crm_push_enabled is True
    yield
    get_settings.cache_clear()


@pytest.fixture
def disable_crm_push(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Turn the CRM push gate OFF (pre-cut-over deployment: no URL / secret)."""
    monkeypatch.setenv("CRM_INGEST_URL", "")
    monkeypatch.setenv("CRM_PUSH_SECRET", "")
    get_settings.cache_clear()
    assert get_settings().crm_push_enabled is False
    yield
    get_settings.cache_clear()


@pytest.fixture
async def synced_account(db_engine: AsyncEngine) -> dict[str, Any]:
    """Seed a user + one mailbox (no group — groups are decommissioned)."""
    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with factory() as ses, ses.begin():
        owner = User(
            username="push_sync_admin",
            role="super_admin",
            password_hash="$argon2id$v=19$m=65536,t=3,p=4$abc$def",
            password_reset_required=False,
        )
        ses.add(owner)
        await ses.flush()
        from backend.app.repositories.mail_accounts import MailAccountsRepo

        repo = MailAccountsRepo(ses)
        new_id = await repo.next_account_id()
        blob = encrypt_mail_password("p", new_id)
        acc = MailAccount(
            id=new_id,
            user_id=owner.id,
            email="pushsync@example.com",
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
        return {"user_id": owner.id, "account_id": acc.id}


def _single_message_box(uid: int) -> Any:
    from datetime import UTC
    from datetime import datetime as _dt

    from worker.app.imap_fetcher import FetchedBox, FetchedMessage

    return FetchedBox(
        uidvalidity=7,
        uidnext=uid + 1,
        new_messages=[
            FetchedMessage(
                uid=uid,
                message_id_header=f"<{uid}@x>",
                from_addr="x@y.com",
                from_name="X",
                to_addrs="pushsync@example.com",
                cc_addrs=None,
                subject="hello",
                internal_date=_dt.now(UTC),
                body_text="hi",
                body_html=None,
                body_truncated=False,
                body_present=True,
                in_reply_to=None,
                refs_header=None,
                x_forwarded_by=None,
                attachments=[],
            )
        ],
    )


def _patch_fetch(monkeypatch: pytest.MonkeyPatch, *, uid: int) -> None:
    """Mock the IMAP boundary only (one fetched message)."""

    async def _fake_to_thread(_func: Any, *_a: Any, **_k: Any) -> Any:
        return _single_message_box(uid)

    monkeypatch.setattr(asyncio, "to_thread", _fake_to_thread)


async def _run_account(db_engine: AsyncEngine, account_id: int) -> Any:
    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with factory() as ses:
        acc = await ses.get(MailAccount, account_id)
    assert acc is not None
    return await sc.sync_one_account(
        acc,
        timeout_seconds=10,
        initial_sync_days=30,
        max_body_bytes=1024,
        max_att_bytes=1024,
    )


def _payloads(items: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for raw in items:
        decoded = raw.decode() if isinstance(raw, bytes) else raw
        out.append(cast(dict[str, Any], json.loads(decoded)))
    return out


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCrmPushEnqueueEnabled:
    async def test_inserted_message_is_enqueued_with_source_sync(
        self,
        db_engine: AsyncEngine,
        enable_crm_push: None,
        synced_account: dict[str, Any],
        redis_client: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _patch_fetch(monkeypatch, uid=220001)

        result = await _run_account(db_engine, synced_account["account_id"])
        assert result.new_count == 1
        assert result.outcome == "ok"

        items = await cast(Any, redis_client.lrange(CRM_PUSH_QUEUE_KEY, 0, -1))
        payloads = _payloads(items)
        assert len(payloads) == 1
        assert payloads[0]["message_id"] > 0
        assert payloads[0]["source"] == "sync"

    async def test_conflicting_uid_is_not_enqueued_twice(
        self,
        db_engine: AsyncEngine,
        enable_crm_push: None,
        synced_account: dict[str, Any],
        redis_client: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Idempotency: only NEWLY inserted rows become outbox items.

        The second cycle re-fetches the same UID; ``ON CONFLICT DO NOTHING`` inserts
        nothing, so nothing may be pushed again (a duplicate push would re-deliver a
        message the CRM already stored).
        """
        _patch_fetch(monkeypatch, uid=220002)
        first = await _run_account(db_engine, synced_account["account_id"])
        assert first.new_count == 1

        second = await _run_account(db_engine, synced_account["account_id"])
        assert second.new_count == 0
        assert second.conflict_count == 1

        items = await cast(Any, redis_client.lrange(CRM_PUSH_QUEUE_KEY, 0, -1))
        assert len(_payloads(items)) == 1  # still exactly ONE outbox item


class TestCrmPushEnqueueDisabled:
    async def test_queue_untouched_when_gate_off(
        self,
        db_engine: AsyncEngine,
        disable_crm_push: None,
        synced_account: dict[str, Any],
        redis_client: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _patch_fetch(monkeypatch, uid=220101)

        result = await _run_account(db_engine, synced_account["account_id"])
        assert result.new_count == 1
        assert result.outcome == "ok"

        # Gate off (pre-cut-over): the message is persisted but nothing is enqueued.
        assert int(await cast(Any, redis_client.llen(CRM_PUSH_QUEUE_KEY))) == 0


class TestCrmPushEnqueueRedisError:
    async def test_redis_error_on_enqueue_is_swallowed(
        self,
        db_engine: AsyncEngine,
        enable_crm_push: None,
        synced_account: dict[str, Any],
        redis_client: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A failing LPUSH must NOT abort the sync (independent try/except, ADR-0043 §2)."""
        _patch_fetch(monkeypatch, uid=220201)

        from backend.app.crm_push import service as crm_push_service

        real = crm_push_service.get_redis()

        class _PushBoomRedis:
            def __init__(self, inner: Any) -> None:
                self._inner = inner

            def __getattr__(self, name: str) -> Any:
                return getattr(self._inner, name)

            async def lpush(self, *_a: Any, **_k: Any) -> int:
                raise RuntimeError("simulated redis outage")

        monkeypatch.setattr(crm_push_service, "get_redis", lambda: _PushBoomRedis(real))

        result = await _run_account(db_engine, synced_account["account_id"])
        # The account sync still succeeds despite the LPUSH failure…
        assert result.new_count == 1
        assert result.outcome == "ok"
        # …and nothing landed on the queue (the LPUSH raised).
        assert int(await cast(Any, redis_client.llen(CRM_PUSH_QUEUE_KEY))) == 0
