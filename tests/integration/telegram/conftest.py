"""Shared fixtures for the integration telegram test package.

Provides:

- :func:`make_init_data` — signs a payload for ``settings.BOT_TOKEN`` so the
  router accepts it.
- :func:`make_admin_user` / :func:`make_user_in_group` — seeded user fixtures
  used by the link / dispatch tests.
- :func:`seed_message_with_tag` — inserts a Message + applies a tag for a
  given recipient so the dispatcher SQL returns them.
- :func:`fake_send_notification` — monkeypatch helper that replaces the
  ``send_notification`` call with a deterministic stub (no network).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from shared.config import get_settings
from shared.models import Group, MailAccount, Message, Tag, User

# ---------------------------------------------------------------------------
# HMAC builder — production code (init_data.py) is the reference; we mirror
# its construction inline so we never call into it from the fixture.
# ---------------------------------------------------------------------------


def _compute_hash(pairs: Iterable[tuple[str, str]], bot_token: str) -> str:
    filtered = [(k, v) for k, v in pairs if k != "hash"]
    filtered.sort(key=lambda kv: kv[0])
    data_check_string = "\n".join(f"{k}={v}" for k, v in filtered)
    secret = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    return hmac.new(secret, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()


def make_init_data(
    *,
    telegram_user_id: int,
    bot_token: str | None = None,
    first_name: str | None = "Tester",
    username: str | None = "tester",
    auth_date: int | None = None,
    tamper_hash: bool = False,
) -> str:
    """Build a verbatim Telegram ``initData`` string signed for ``bot_token``.

    ``bot_token`` defaults to ``settings.BOT_TOKEN`` (i.e. the value the
    production verifier reads), so the router accepts it as authentic.
    """
    s = get_settings()
    token = bot_token if bot_token is not None else s.BOT_TOKEN
    if auth_date is None:
        auth_date = int(time.time())
    user_json = json.dumps(
        {
            "id": int(telegram_user_id),
            **({"first_name": first_name} if first_name is not None else {}),
            **({"username": username} if username is not None else {}),
        },
        separators=(",", ":"),
    )
    pairs: list[tuple[str, str]] = [
        ("query_id", "QID_TEST"),
        ("user", user_json),
        ("auth_date", str(auth_date)),
    ]
    h = _compute_hash(pairs, token)
    if tamper_hash:
        h = ("0" if h[-1] != "0" else "1").join((h[:-1], ""))
    pairs.append(("hash", h))
    return "&".join(f"{k}={quote(v, safe='')}" for k, v in pairs)


# ---------------------------------------------------------------------------
# DB seeding helpers
# ---------------------------------------------------------------------------


async def _ensure_super_admin(db_engine: AsyncEngine) -> User:
    """Return the seeded super-admin user. App startup seed creates it."""
    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    s = get_settings()
    async with factory() as ses:
        u = (
            await ses.execute(select(User).where(User.username == s.ADMIN_LOGIN))
        ).scalar_one_or_none()
        assert (
            u is not None
        ), "super-admin was not seeded; the app startup must run before this fixture."
        return u


async def _create_group_with_leader(
    db_engine: AsyncEngine,
    *,
    group_name: str,
    leader_username: str,
    leader_password_hash: str | None = None,
) -> tuple[Group, User]:
    """Create a group with a fresh leader user (group_leader role)."""
    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with factory() as ses, ses.begin():
        leader = User(
            username=leader_username,
            display_name=leader_username,
            role="group_leader",
            password_hash=leader_password_hash,
            password_reset_required=leader_password_hash is None,
        )
        ses.add(leader)
        await ses.flush()
        group = Group(name=group_name, leader_user_id=leader.id)
        ses.add(group)
        await ses.flush()
        leader.group_id = group.id
        await ses.flush()
        # Detach so subsequent reads don't return stale objects.
        await ses.refresh(group)
        await ses.refresh(leader)
        return group, leader


async def _create_member(
    db_engine: AsyncEngine,
    *,
    group_id: int,
    username: str,
    password_hash: str | None = None,
) -> User:
    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with factory() as ses, ses.begin():
        u = User(
            username=username,
            display_name=username,
            role="group_member",
            group_id=group_id,
            password_hash=password_hash,
            password_reset_required=password_hash is None,
        )
        ses.add(u)
        await ses.flush()
        await ses.refresh(u)
        return u


# ---------------------------------------------------------------------------
# Reusable fake send_notification
# ---------------------------------------------------------------------------


@dataclass
class FakeSendResult:
    """One outcome the fake will return. Iterates per call."""

    kind: str  # ok / dead / retry_after / transient / disabled
    telegram_message_id: int | None = None
    retry_after_sec: int | None = None
    detail: str | None = None


@dataclass
class FakeSendNotificationRecorder:
    """Records every call made to the stub + drives the response queue."""

    calls: list[dict[str, Any]] = field(default_factory=list)
    # The script of outcomes (FIFO). Last entry is repeated if the queue
    # is shorter than the number of calls.
    script: list[FakeSendResult] = field(default_factory=list)

    def push(self, *outcomes: FakeSendResult) -> None:
        self.script.extend(outcomes)

    async def __call__(self, *, chat_id: int, text_html: str, message_id: int) -> Any:
        # Import here to avoid a hard dependency at module load.
        from backend.app.telegram.bot import SendNotificationResult

        self.calls.append({"chat_id": chat_id, "text_html": text_html, "message_id": message_id})
        if self.script:
            outcome = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        else:
            outcome = FakeSendResult(kind="ok", telegram_message_id=12345)
        return SendNotificationResult(
            kind=outcome.kind,  # type: ignore[arg-type]
            telegram_message_id=outcome.telegram_message_id,
            retry_after_sec=outcome.retry_after_sec,
            detail=outcome.detail,
        )


@pytest.fixture
def fake_send_notification(
    monkeypatch: pytest.MonkeyPatch,
) -> FakeSendNotificationRecorder:
    """Replace :func:`send_notification` everywhere it is imported.

    Notes:
    - The notify_service imports the function via
      ``from backend.app.telegram.bot import send_notification`` and uses it
      directly, so patching the source module's symbol is sufficient
      — Python re-imports look up the module attribute at call time only if
      the consumer does ``module.send_notification``; ``from`` imports bind
      the symbol. Therefore we ALSO patch the consumer's namespace
      (notify_service.send_notification).
    """
    recorder = FakeSendNotificationRecorder()
    monkeypatch.setattr("backend.app.telegram.bot.send_notification", recorder, raising=True)
    monkeypatch.setattr(
        "backend.app.telegram.notify_service.send_notification",
        recorder,
        raising=True,
    )
    return recorder


# ---------------------------------------------------------------------------
# Common pytest fixtures (re-exported for the test files)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def super_admin_user(db_engine: AsyncEngine) -> User:
    return await _ensure_super_admin(db_engine)


@pytest_asyncio.fixture
async def leader_and_group(db_engine: AsyncEngine) -> tuple[Group, User]:
    return await _create_group_with_leader(
        db_engine, group_name="Test Group", leader_username="leader_tg"
    )


CreateMemberCallable = Callable[..., Any]


@pytest_asyncio.fixture
def create_member(db_engine: AsyncEngine) -> CreateMemberCallable:
    async def _create(group_id: int, username: str, password_hash: str | None = None) -> User:
        return await _create_member(
            db_engine,
            group_id=group_id,
            username=username,
            password_hash=password_hash,
        )

    return _create


async def _create_mail_account_for_user(
    db_engine: AsyncEngine,
    *,
    user_id: int,
    email: str,
    display_name: str | None = None,
    group_id: int | None = None,
) -> MailAccount:
    """Create a ``mail_accounts`` row owned by ``user_id``.

    ``group_id`` is set to ``user.group_id`` by default — matches what the
    real account-creation flow does. The dispatcher's recipient SQL joins on
    ``mail_accounts.group_id``, so this is load-bearing.
    """
    from shared.crypto import encrypt_mail_password

    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with factory() as ses, ses.begin():
        from backend.app.repositories.mail_accounts import MailAccountsRepo

        repo = MailAccountsRepo(ses)
        new_id = await repo.next_account_id()
        blob = encrypt_mail_password("p", new_id)
        if group_id is None:
            # Use the owner's group_id if available.
            owner = await ses.get(User, user_id)
            group_id = owner.group_id if owner is not None else None
        acc = MailAccount(
            id=new_id,
            user_id=user_id,
            group_id=group_id,
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


@pytest_asyncio.fixture
def create_mail_account(db_engine: AsyncEngine) -> Callable[..., Any]:
    async def _create(
        user_id: int,
        email: str,
        *,
        display_name: str | None = None,
        group_id: int | None = None,
    ) -> MailAccount:
        return await _create_mail_account_for_user(
            db_engine,
            user_id=user_id,
            email=email,
            display_name=display_name,
            group_id=group_id,
        )

    return _create


async def _create_message(
    db_engine: AsyncEngine,
    *,
    mail_account_id: int,
    uid: int,
    subject: str = "Hello",
    from_addr: str = "sender@x.com",
    from_name: str | None = "Sender Name",
    internal_date: datetime | None = None,
) -> Message:
    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with factory() as ses, ses.begin():
        m = Message(
            mail_account_id=mail_account_id,
            uid=uid,
            uidvalidity=1,
            from_addr=from_addr,
            from_name=from_name,
            to_addrs="me@example.com",
            subject=subject,
            internal_date=internal_date or datetime.now(UTC),
            body_text="body",
        )
        ses.add(m)
        await ses.flush()
        await ses.refresh(m)
        return m


@pytest_asyncio.fixture
def create_message(db_engine: AsyncEngine) -> Callable[..., Any]:
    async def _create(
        mail_account_id: int,
        uid: int,
        *,
        subject: str = "Hello",
        from_addr: str = "sender@x.com",
        from_name: str | None = "Sender Name",
        internal_date: datetime | None = None,
    ) -> Message:
        return await _create_message(
            db_engine,
            mail_account_id=mail_account_id,
            uid=uid,
            subject=subject,
            from_addr=from_addr,
            from_name=from_name,
            internal_date=internal_date,
        )

    return _create


async def _create_tag_and_link_message(
    db_engine: AsyncEngine,
    *,
    user_id: int,
    message_id: int,
    name: str,
    color: str = "#aabbcc",
) -> Tag:
    """Create a tag owned by ``user_id`` and link it to ``message_id``."""
    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with factory() as ses, ses.begin():
        from shared.models import MessageTag

        tag = Tag(user_id=user_id, name=name, color=color)
        ses.add(tag)
        await ses.flush()
        ses.add(MessageTag(message_id=message_id, tag_id=tag.id))
        await ses.flush()
        await ses.refresh(tag)
        return tag


@pytest_asyncio.fixture
def tag_message_for_user(db_engine: AsyncEngine) -> Callable[..., Any]:
    async def _link(
        user_id: int,
        message_id: int,
        name: str,
        color: str = "#aabbcc",
    ) -> Tag:
        return await _create_tag_and_link_message(
            db_engine,
            user_id=user_id,
            message_id=message_id,
            name=name,
            color=color,
        )

    return _link


# ---------------------------------------------------------------------------
# TelegramLink helpers
# ---------------------------------------------------------------------------


async def _create_telegram_link(
    db_engine: AsyncEngine,
    *,
    telegram_user_id: int,
    user_id: int,
    dead_at: datetime | None = None,
) -> None:
    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with factory() as ses, ses.begin():
        from shared.models import TelegramLink

        link = TelegramLink(telegram_user_id=telegram_user_id, user_id=user_id, dead_at=dead_at)
        ses.add(link)
        await ses.flush()


@pytest_asyncio.fixture
def make_link(db_engine: AsyncEngine) -> Callable[..., Any]:
    async def _link(telegram_user_id: int, user_id: int, *, dead: bool = False) -> None:
        await _create_telegram_link(
            db_engine,
            telegram_user_id=telegram_user_id,
            user_id=user_id,
            dead_at=datetime.now(UTC) if dead else None,
        )

    return _link


# ---------------------------------------------------------------------------
# Helpers exposed to tests
# ---------------------------------------------------------------------------


__all__ = [
    "FakeSendNotificationRecorder",
    "FakeSendResult",
    "create_mail_account",
    "create_member",
    "create_message",
    "fake_send_notification",
    "leader_and_group",
    "make_init_data",
    "make_link",
    "super_admin_user",
    "tag_message_for_user",
]
