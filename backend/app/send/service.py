"""SendService — compose / reply via SMTP, persist, best-effort IMAP append.

Algorithm follows ``docs/01-architecture.md`` sequence S4 and
``docs/05-modules.md`` sec. 11.
"""

from __future__ import annotations

import asyncio
import contextlib
import ssl

import aiosmtplib
import imap_tools
from imap_tools import MailBoxUnencrypted
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.deps import VisibilityScope
from backend.app.exceptions import (
    NotFoundError,
    SMTPSendFailedError,
)
from backend.app.repositories.mail_accounts import MailAccountsRepo
from backend.app.repositories.messages import MessagesRepo
from backend.app.repositories.sent_messages import SentMessagesRepo
from backend.app.repositories.users import UsersRepo
from backend.app.security import assert_public_host
from backend.app.send.mime import build_mime, generate_message_id
from backend.app.send.schemas import SendMessageRequest, SendMessageResponse
from shared.crypto import decrypt_mail_password
from shared.logging import get_logger

log = get_logger(__name__)

_SMTP_TIMEOUT = 60
_IMAP_APPEND_TIMEOUT = 30


def _ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


def _safe_error_text(exc: BaseException, max_len: int = 200) -> str:
    return str(exc).replace("\r", " ").replace("\n", " ")[:max_len]


def _imap_append_blocking(
    *,
    host: str,
    port: int,
    ssl_on: bool,
    username: str,
    password: str,
    raw_message_bytes: bytes,
) -> None:
    """Append raw RFC822 bytes to the IMAP "Sent" folder, best-effort.

    Different providers name the Sent folder differently (``Sent``,
    ``[Gmail]/Sent Mail``, etc.). We try a small set; the first that
    appends without error wins.

    ``imap_tools.MailBox(...)`` connects in its constructor, so DNS resolution
    and the TCP/TLS handshake happen there. The constructor and :meth:`login`
    therefore live inside the try/finally so that ``socket.gaierror`` (an
    :class:`OSError` subclass) does not escape as an unhandled exception, and
    so :meth:`logout` is only attempted when ``mailbox`` was actually built.
    """
    mailbox: imap_tools.BaseMailBox | None = None
    try:
        if ssl_on:
            mailbox = imap_tools.MailBox(host, port=port, timeout=_IMAP_APPEND_TIMEOUT)
        else:
            mailbox = MailBoxUnencrypted(host, port=port, timeout=_IMAP_APPEND_TIMEOUT)
        mailbox.login(username, password)

        candidates = ("Sent", "[Gmail]/Sent Mail", "INBOX.Sent", "Sent Items")
        last_exc: Exception | None = None
        for folder in candidates:
            try:
                mailbox.append(
                    raw_message_bytes,
                    folder,
                    flag_set=("\\Seen",),
                )
                return
            except imap_tools.MailboxAppendError as exc:
                last_exc = exc
                continue
        if last_exc is not None:
            raise last_exc
    finally:
        if mailbox is not None:
            with contextlib.suppress(Exception):
                mailbox.logout()


class SendService:
    def __init__(self, session: AsyncSession) -> None:
        self._db = session
        self._accounts = MailAccountsRepo(session)
        self._messages = MessagesRepo(session)
        self._sent = SentMessagesRepo(session)
        self._users = UsersRepo(session)

    async def _visible_user_ids(self, scope: VisibilityScope) -> list[int] | None:
        """FE-FIX round-10: returns visible ``mail_accounts.id`` (semantic
        change kept under the legacy method name to avoid renaming callers).
        """
        if scope.is_super_admin:
            return None
        return await self._accounts.list_account_ids_visible(
            group_id=scope.group_id, owner_user_id=scope.user_id
        )

    async def send(
        self, *, scope: VisibilityScope, payload: SendMessageRequest
    ) -> SendMessageResponse:
        # 1. Visibility: from_account must be reachable by the caller's
        #    scope (super-admin sees all; group_leader/group_member see
        #    every member's mailboxes — ADR-0019 §7.1, §8).
        visible = await self._visible_user_ids(scope)
        acc = await self._accounts.get_for_user_ids(visible, payload.from_account_id)
        if acc is None:
            raise NotFoundError("Mail account not found")

        # 2. Resolve in-reply-to headers if requested.
        in_reply_header: str | None = None
        refs_header: str | None = None
        if payload.in_reply_to_message_id is not None:
            original = await self._messages.get_for_user_ids(
                message_id=payload.in_reply_to_message_id,
                mail_account_ids=visible,
            )
            if original is None:
                raise NotFoundError("Original message not found")
            if original.message_id_header:
                in_reply_header = original.message_id_header
                # References = old refs + original Message-ID, per RFC 5322.
                if original.refs_header:
                    refs_header = original.refs_header.strip() + " " + original.message_id_header
                else:
                    refs_header = original.message_id_header

        # 3. Decrypt SMTP password (falls back to IMAP password if separate
        #    SMTP creds aren't set).
        smtp_user = acc.smtp_username or acc.email
        if acc.smtp_encrypted_password is not None:
            smtp_pwd = decrypt_mail_password(acc.smtp_encrypted_password, acc.id)
        else:
            smtp_pwd = decrypt_mail_password(acc.encrypted_password, acc.id)

        # 4. Build MIME.
        message_id = generate_message_id()
        msg = build_mime(
            from_addr=acc.email,
            to=payload.to,
            cc=payload.cc,
            bcc=payload.bcc,
            subject=payload.subject,
            body_text=payload.body,
            in_reply_to_header=in_reply_header,
            references_header=refs_header,
            message_id=message_id,
        )

        recipients = list(payload.to)
        if payload.cc:
            recipients.extend(payload.cc)
        if payload.bcc:
            recipients.extend(payload.bcc)

        # 5. SMTP send.
        assert_public_host(acc.smtp_host, port=acc.smtp_port)
        try:
            await aiosmtplib.send(
                msg,
                hostname=acc.smtp_host,
                port=acc.smtp_port,
                username=smtp_user,
                password=smtp_pwd,
                use_tls=acc.smtp_ssl,
                start_tls=acc.smtp_starttls,
                tls_context=_ssl_context(),
                recipients=recipients,
                timeout=_SMTP_TIMEOUT,
            )
        except aiosmtplib.SMTPException as exc:
            raise SMTPSendFailedError(
                "SMTP send failed",
                details={"detail": _safe_error_text(exc)},
            ) from exc
        except (TimeoutError, OSError) as exc:
            raise SMTPSendFailedError(
                "SMTP send failed",
                details={"detail": _safe_error_text(exc)},
            ) from exc

        # 6. Best-effort IMAP APPEND to the Sent folder.
        appended = False
        appended_error: str | None = None
        try:
            assert_public_host(acc.imap_host, port=acc.imap_port)
            await asyncio.wait_for(
                asyncio.to_thread(
                    _imap_append_blocking,
                    host=acc.imap_host,
                    port=acc.imap_port,
                    ssl_on=acc.imap_ssl,
                    username=acc.email,
                    password=decrypt_mail_password(acc.encrypted_password, acc.id),
                    raw_message_bytes=bytes(msg),
                ),
                timeout=_IMAP_APPEND_TIMEOUT + 5,
            )
            appended = True
        except Exception as exc:  # — best-effort
            appended_error = _safe_error_text(exc)
            log.info(
                "smtp_send_imap_append_failed",
                mail_account_id=acc.id,
                detail=appended_error,
            )

        # 7. Persist sent_messages. ``user_id`` records the *author*
        # (the caller) — distinct from the mailbox owner ``acc.user_id``
        # so a leader sending from a member's mailbox is correctly
        # attributed (ADR-0019 §7.3).
        sent = await self._sent.insert(
            user_id=scope.user_id,
            from_account_id=acc.id,
            to_addrs=", ".join(payload.to),
            cc_addrs=", ".join(payload.cc) if payload.cc else None,
            bcc_addrs=", ".join(payload.bcc) if payload.bcc else None,
            subject=payload.subject,
            body_text=payload.body,
            in_reply_to=in_reply_header,
            refs_header=refs_header,
            smtp_message_id=message_id,
            appended_to_sent=appended,
            appended_error=appended_error,
        )

        return SendMessageResponse(
            sent_id=sent.id,
            smtp_message_id=message_id,
            appended_to_sent=appended,
        )
