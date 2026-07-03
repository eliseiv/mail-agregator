"""ADR-0027 §3.1 — sync_cycle push-queue enqueue behaviour.

Source of truth: ``worker/app/sync_cycle.py`` (the third independent
``if settings.push_team_bots_enabled:`` block after the TG + webhook enqueue).

We drive ``sync_one_account`` with a mocked IMAP fetch (one inserted message)
and a mocked tag-apply, then assert on the real Redis ``push_notify_queue``.
The TG + webhook enqueues are stubbed so they neither touch Redis nor fail —
this test isolates the push branch.

Covered:
- enabled → LPUSH push_notify_queue with the SAME message_id(s) as the main
  channel; the main ``tg_notify_queue`` is independently enqueued (real call).
- disabled → push_notify_queue untouched.
- a Redis error on the push LPUSH is swallowed (the per-account commit and the
  main enqueue path are unaffected; ``sync_one_account`` returns ``ok``).
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from shared.config import get_settings
from shared.crypto import encrypt_mail_password
from shared.models import MailAccount, User
from worker.app import push_notify_dispatch as pnd
from worker.app import sync_cycle as sc

pytestmark = pytest.mark.integration  # needs DB + Redis

_PUSH_QUEUE = pnd._QUEUE_KEY
_TG_QUEUE = "tg_notify_queue"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def enable_push(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("BOT_IVAN_TOKEN", "IVAN_TOK")
    monkeypatch.setenv("BOT_IVAN_GROUP_ID", "1")
    monkeypatch.setenv("ADMIN_TELEGRAM_IDS", "111,222")
    get_settings.cache_clear()
    assert get_settings().push_team_bots_enabled is True
    yield
    get_settings.cache_clear()


@pytest.fixture
def disable_push(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv("BOT_IVAN_TOKEN", raising=False)
    monkeypatch.delenv("BOT_ALEXANDRA_TOKEN", raising=False)
    monkeypatch.delenv("BOT_ANDREI_TOKEN", raising=False)
    monkeypatch.delenv("ADMIN_TELEGRAM_IDS", raising=False)
    get_settings.cache_clear()
    assert get_settings().push_team_bots_enabled is False
    yield
    get_settings.cache_clear()


@pytest.fixture
async def account_in_group1(db_engine: AsyncEngine) -> dict[str, Any]:
    """Seed a user + a mail account whose group_id is 1 (ivan's team)."""
    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with factory() as ses, ses.begin():
        from shared.models import Group

        # group_id=1 is ivan's team (FK target for the account below).
        ses.add(Group(id=1, name="team1", leader_user_id=None))
        admin = User(
            username="push_sync_admin",
            role="super_admin",
            password_hash="$argon2id$v=19$m=65536,t=3,p=4$abc$def",
            password_reset_required=False,
        )
        ses.add(admin)
        await ses.flush()
        from backend.app.repositories.mail_accounts import MailAccountsRepo

        repo = MailAccountsRepo(ses)
        new_id = await repo.next_account_id()
        blob = encrypt_mail_password("p", new_id)
        acc = MailAccount(
            id=new_id,
            user_id=admin.id,
            group_id=1,
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
        return {"user_id": admin.id, "account_id": acc.id}


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


def _patch_fetch_and_tags(monkeypatch: pytest.MonkeyPatch, *, uid: int) -> None:
    """Mock IMAP fetch (one message) + a tag-apply that records the message id."""

    async def _fake_to_thread(_func: Any, *_a: Any, **_k: Any) -> Any:
        return _single_message_box(uid)

    monkeypatch.setattr(asyncio, "to_thread", _fake_to_thread)

    from backend.app.tags.service import TagsService

    async def _fake_apply(self: Any, *, message: Any, mail_account_id: int) -> int:
        return 0  # 0 tags; TG_NOTIFY_ALL_MESSAGES default-true still enqueues

    monkeypatch.setattr(TagsService, "apply_tags_to_message", _fake_apply)


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


def _decode_message_ids(items: list[Any]) -> list[int]:
    import json

    out: list[int] = []
    for raw in items:
        decoded = raw.decode() if isinstance(raw, bytes) else raw
        out.append(json.loads(decoded)["message_id"])
    return out


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestPushEnqueueEnabled:
    async def test_push_queue_gets_same_message_ids_as_main(
        self,
        db_engine: AsyncEngine,
        enable_push: None,
        account_in_group1: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _patch_fetch_and_tags(monkeypatch, uid=220001)

        result = await _run_account(db_engine, account_in_group1["account_id"])
        assert result.new_count == 1
        assert result.outcome == "ok"

        from shared.redis_client import get_redis

        r = get_redis()
        push_items = await r.lrange(_PUSH_QUEUE, 0, -1)
        tg_items = await r.lrange(_TG_QUEUE, 0, -1)

        push_ids = _decode_message_ids(push_items)
        tg_ids = _decode_message_ids(tg_items)

        # Exactly one message enqueued, identical id on both queues.
        assert len(push_ids) == 1
        assert push_ids == tg_ids
        assert push_ids[0] > 0

    async def test_push_payload_uses_source_sync(
        self,
        db_engine: AsyncEngine,
        enable_push: None,
        account_in_group1: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import json

        _patch_fetch_and_tags(monkeypatch, uid=220002)
        await _run_account(db_engine, account_in_group1["account_id"])

        from shared.redis_client import get_redis

        r = get_redis()
        items = await r.lrange(_PUSH_QUEUE, 0, -1)
        assert len(items) == 1
        raw = items[0]
        payload = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
        assert payload["source"] == "sync"


class TestPushEnqueueDisabled:
    async def test_push_queue_untouched_when_disabled_main_still_enqueues(
        self,
        db_engine: AsyncEngine,
        disable_push: None,
        account_in_group1: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _patch_fetch_and_tags(monkeypatch, uid=220101)

        result = await _run_account(db_engine, account_in_group1["account_id"])
        assert result.outcome == "ok"

        from shared.redis_client import get_redis

        r = get_redis()
        # push queue stays empty…
        assert await r.llen(_PUSH_QUEUE) == 0
        # …while the main TG queue is still enqueued (feature is independent).
        assert await r.llen(_TG_QUEUE) == 1


class TestPushEnqueueRedisError:
    async def test_redis_error_on_push_enqueue_is_swallowed(
        self,
        db_engine: AsyncEngine,
        enable_push: None,
        account_in_group1: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A failing push LPUSH must NOT break the account sync or the main
        enqueue (independent try/except, ADR-0027 §3.1)."""
        _patch_fetch_and_tags(monkeypatch, uid=220201)

        # Patch the redis client used by sync_cycle so ONLY lpush raises. We
        # wrap the real client and explode on lpush (the push branch), while
        # leaving every other method intact so the TG/webhook paths still work.
        from shared.redis_client import get_redis as _real_get_redis

        real = _real_get_redis()

        class _PushBoomRedis:
            def __init__(self, inner: Any) -> None:
                self._inner = inner

            def __getattr__(self, name: str) -> Any:
                return getattr(self._inner, name)

            async def lpush(self, *_a: Any, **_k: Any) -> int:
                raise RuntimeError("simulated push redis outage")

        monkeypatch.setattr(sc, "get_redis", lambda: _PushBoomRedis(real))

        result = await _run_account(db_engine, account_in_group1["account_id"])
        # The account sync still succeeds despite the push LPUSH failure.
        assert result.new_count == 1
        assert result.outcome == "ok"

        # The push queue is empty (LPUSH raised) but the message WAS persisted
        # and the main TG queue still got the id (separate code path).
        r = real
        assert await r.llen(_PUSH_QUEUE) == 0
        assert await r.llen(_TG_QUEUE) == 1
