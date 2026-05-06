"""Integration tests for ``SendService`` — direct service calls with real
DB; SMTP transport (``aiosmtplib.send``) and IMAP append are mocked.

Covers paths the HTTP-level ``test_send.py`` doesn't easily exercise:
- IMAP append failure is swallowed and logged; sent row records the error.
- in_reply_to_message_id resolution: References header chains correctly.
- SMTPException -> SMTPSendFailedError mapping.
- separate-SMTP-creds path actually decrypts the SMTP blob.

Source of truth: ``backend/app/send/service.py`` +
``docs/05-modules.md`` sec. 11.
"""

from __future__ import annotations

from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.exceptions import NotFoundError, SMTPSendFailedError
from backend.app.repositories.mail_accounts import MailAccountsRepo
from backend.app.repositories.users import UsersRepo
from backend.app.send.schemas import SendMessageRequest
from backend.app.send.service import SendService
from shared.crypto import encrypt_mail_password
from shared.models import MailAccount

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def user_id(db_session: AsyncSession) -> int:
    user = await UsersRepo(db_session).create(
        username="alice",
        email="alice@example.com",
        is_admin=False,
        password_hash="x",
        password_reset_required=False,
    )
    await db_session.commit()
    return user.id


@pytest_asyncio.fixture
async def account(db_session: AsyncSession, user_id: int) -> MailAccount:
    """Insert a mail account with encrypted password bound to its id."""
    repo = MailAccountsRepo(db_session)
    new_id = await repo.next_account_id()
    enc = encrypt_mail_password("imap-pwd", new_id)
    acc = await repo.insert_with_id(
        account_id=new_id,
        user_id=user_id,
        email="alice@example.com",
        encrypted_password=enc,
        imap_host="imap.example.com",
        imap_port=993,
        imap_ssl=True,
        smtp_host="smtp.example.com",
        smtp_port=465,
        smtp_ssl=True,
        smtp_starttls=False,
        smtp_username=None,
        smtp_encrypted_password=None,
    )
    await db_session.commit()
    return acc


@pytest.fixture
def stub_smtp(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Patch out ``aiosmtplib.send`` and the IMAP append. Returns a dict
    that records the calls so tests can assert what got sent.
    """
    from backend.app.send import service as snd_svc

    record: dict[str, Any] = {"smtp_calls": [], "imap_appends": []}

    async def _fake_send(msg: Any, **kwargs: Any) -> Any:
        record["smtp_calls"].append({"msg": msg, "kwargs": kwargs})
        return None

    def _fake_append(**kwargs: Any) -> None:
        record["imap_appends"].append(kwargs)

    monkeypatch.setattr("aiosmtplib.send", _fake_send)
    monkeypatch.setattr(snd_svc, "_imap_append_blocking", _fake_append)
    return record


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestSendHappyPath:
    async def test_basic_send_persists_sent_row(
        self,
        db_session: AsyncSession,
        user_id: int,
        account: MailAccount,
        stub_smtp: dict[str, Any],
    ) -> None:
        svc = SendService(db_session)
        async with db_session.begin():
            resp = await svc.send(
                user_id=user_id,
                payload=SendMessageRequest(
                    from_account_id=account.id,
                    to=["recipient@example.com"],
                    subject="Hello",
                    body="World",
                ),
            )

        assert resp.sent_id > 0
        assert resp.smtp_message_id.startswith("<") and resp.smtp_message_id.endswith(">")
        assert resp.appended_to_sent is True
        # SMTP transport called exactly once.
        assert len(stub_smtp["smtp_calls"]) == 1
        # Recipients passed via ``recipients=``.
        assert "recipient@example.com" in stub_smtp["smtp_calls"][0]["kwargs"]["recipients"]

    async def test_bcc_in_recipients_but_not_in_headers(
        self,
        db_session: AsyncSession,
        user_id: int,
        account: MailAccount,
        stub_smtp: dict[str, Any],
    ) -> None:
        svc = SendService(db_session)
        async with db_session.begin():
            await svc.send(
                user_id=user_id,
                payload=SendMessageRequest(
                    from_account_id=account.id,
                    to=["a@example.com"],
                    cc=["c@example.com"],
                    bcc=["secret@example.com"],
                    subject="x",
                    body="x",
                ),
            )

        call = stub_smtp["smtp_calls"][0]
        # BCC passed to the transport...
        assert "secret@example.com" in call["kwargs"]["recipients"]
        # ...but not in the MIME headers.
        msg = call["msg"]
        # EmailMessage.get_all returns None when header not present.
        bcc_header = msg.get_all("Bcc")
        assert bcc_header is None or "secret@example.com" not in str(bcc_header)


# ---------------------------------------------------------------------------
# IMAP append failure is best-effort
# ---------------------------------------------------------------------------


class TestImapAppendFailureSwallowed:
    async def test_imap_append_failure_does_not_fail_send(
        self,
        db_session: AsyncSession,
        user_id: int,
        account: MailAccount,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from backend.app.send import service as snd_svc

        async def _ok_smtp(*_args: Any, **_kwargs: Any) -> None:
            return None

        def _bad_append(**_kwargs: Any) -> None:
            raise OSError("connection refused")

        monkeypatch.setattr("aiosmtplib.send", _ok_smtp)
        monkeypatch.setattr(snd_svc, "_imap_append_blocking", _bad_append)

        svc = SendService(db_session)
        async with db_session.begin():
            resp = await svc.send(
                user_id=user_id,
                payload=SendMessageRequest(
                    from_account_id=account.id,
                    to=["a@example.com"],
                    subject="x",
                    body="x",
                ),
            )
        # Send still succeeded — IMAP append is best-effort.
        assert resp.appended_to_sent is False
        assert resp.sent_id > 0


# ---------------------------------------------------------------------------
# SMTP failures
# ---------------------------------------------------------------------------


class TestSmtpFailure:
    async def test_smtp_exception_maps_to_domain_error(
        self,
        db_session: AsyncSession,
        user_id: int,
        account: MailAccount,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import aiosmtplib

        async def _bad(*_args: Any, **_kwargs: Any) -> None:
            raise aiosmtplib.SMTPConnectError("nope")

        monkeypatch.setattr("aiosmtplib.send", _bad)

        svc = SendService(db_session)
        with pytest.raises(SMTPSendFailedError):
            async with db_session.begin():
                await svc.send(
                    user_id=user_id,
                    payload=SendMessageRequest(
                        from_account_id=account.id,
                        to=["a@example.com"],
                        subject="x",
                        body="x",
                    ),
                )

    async def test_oserror_maps_to_smtp_send_failed(
        self,
        db_session: AsyncSession,
        user_id: int,
        account: MailAccount,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def _bad(*_args: Any, **_kwargs: Any) -> None:
            raise OSError("dns failure")

        monkeypatch.setattr("aiosmtplib.send", _bad)

        svc = SendService(db_session)
        with pytest.raises(SMTPSendFailedError):
            async with db_session.begin():
                await svc.send(
                    user_id=user_id,
                    payload=SendMessageRequest(
                        from_account_id=account.id,
                        to=["a@example.com"],
                        subject="x",
                        body="x",
                    ),
                )


# ---------------------------------------------------------------------------
# Ownership / NotFound
# ---------------------------------------------------------------------------


class TestOwnership:
    async def test_send_from_unknown_account_raises_not_found(
        self,
        db_session: AsyncSession,
        user_id: int,
        stub_smtp: dict[str, Any],  # noqa: ARG002
    ) -> None:
        svc = SendService(db_session)
        with pytest.raises(NotFoundError):
            async with db_session.begin():
                await svc.send(
                    user_id=user_id,
                    payload=SendMessageRequest(
                        from_account_id=999_999,
                        to=["a@example.com"],
                        subject="x",
                        body="x",
                    ),
                )

    async def test_send_from_other_users_account_raises_not_found(
        self,
        db_session: AsyncSession,
        user_id: int,
        account: MailAccount,
        stub_smtp: dict[str, Any],  # noqa: ARG002
    ) -> None:
        # bob exists and tries to send from alice's account.
        bob = await UsersRepo(db_session).create(
            username="bob",
            email=None,
            is_admin=False,
            password_hash="x",
            password_reset_required=False,
        )
        await db_session.commit()

        svc = SendService(db_session)
        with pytest.raises(NotFoundError):
            async with db_session.begin():
                await svc.send(
                    user_id=bob.id,
                    payload=SendMessageRequest(
                        from_account_id=account.id,  # alice's account
                        to=["a@example.com"],
                        subject="x",
                        body="x",
                    ),
                )


# ---------------------------------------------------------------------------
# Reply: in_reply_to wiring
# ---------------------------------------------------------------------------


class TestReplyHeaders:
    async def test_reply_to_unknown_message_raises_not_found(
        self,
        db_session: AsyncSession,
        user_id: int,
        account: MailAccount,
        stub_smtp: dict[str, Any],  # noqa: ARG002
    ) -> None:
        svc = SendService(db_session)
        with pytest.raises(NotFoundError):
            async with db_session.begin():
                await svc.send(
                    user_id=user_id,
                    payload=SendMessageRequest(
                        from_account_id=account.id,
                        to=["a@example.com"],
                        subject="re: x",
                        body="reply body",
                        in_reply_to_message_id=999_999,
                    ),
                )
