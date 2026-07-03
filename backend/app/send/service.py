"""SendService — compose / reply via SMTP, persist, best-effort IMAP append.

Algorithm follows ``docs/01-architecture.md`` sequence S4 and
``docs/05-modules.md`` sec. 11.
"""

from __future__ import annotations

import asyncio
import contextlib
import ssl
from email.message import EmailMessage

import aiosmtplib
import imap_tools
from imap_tools import MailBoxUnencrypted
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.accounts.testers import build_xoauth2_string
from backend.app.deps import VisibilityScope
from backend.app.exceptions import (
    NotFoundError,
    OAuthReconsentRequiredError,
    SMTPSendFailedError,
)
from backend.app.oauth.service import OutlookTokenService
from backend.app.repositories.mail_accounts import MailAccountsRepo
from backend.app.repositories.messages import MessagesRepo
from backend.app.repositories.sent_messages import SentMessagesRepo
from backend.app.repositories.users import UsersRepo
from backend.app.security import assert_public_host
from backend.app.send.mime import build_mime, generate_message_id
from backend.app.send.schemas import SendMessageRequest, SendMessageResponse
from shared.crypto import decrypt_mail_password
from shared.logging import get_logger
from shared.models import MailAccount

log = get_logger(__name__)

# ADR-0033 / ADR-0032 follow-up: fail-fast SMTP timeout. A hung or blocked
# SMTP connect (e.g. provider silently drops outbound :465, or the server
# never answers the banner) must surface as a domain error long before
# nginx ``proxy_read_timeout`` (60s) turns it into a 504. Invariant (QA §21):
# ``_SMTP_TIMEOUT <= 25`` AND ``_SMTP_TIMEOUT + _IMAP_APPEND_TIMEOUT + 5 < 60``
# (20 + 30 + 5 = 55 < 60). ``aiosmtplib`` applies this single timeout to the
# whole connect+STARTTLS+AUTH+DATA sequence, so a stuck connect raises
# ``SMTPConnectError`` / ``SMTPTimeoutError`` (both ``SMTPException``
# subclasses) within 20s → mapped to ``SMTPSendFailedError`` below.
_SMTP_TIMEOUT = 20
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


def _imap_append_oauth_blocking(
    *,
    host: str,
    port: int,
    email: str,
    access_token: str,
    raw_message_bytes: bytes,
) -> None:
    """Append raw RFC822 bytes to the Outlook "Sent" folder via XOAUTH2 (ADR-0025).

    Same best-effort folder-probe as :func:`_imap_append_blocking` but
    authenticates with SASL XOAUTH2 (``MailBox.xoauth2``) instead of LOGIN.
    """
    mailbox: imap_tools.BaseMailBox | None = None
    try:
        mailbox = imap_tools.MailBox(host, port=port, timeout=_IMAP_APPEND_TIMEOUT)
        mailbox.xoauth2(email, access_token)
        candidates = ("Sent", "Sent Items", "[Gmail]/Sent Mail", "INBOX.Sent")
        last_exc: Exception | None = None
        for folder in candidates:
            try:
                mailbox.append(raw_message_bytes, folder, flag_set=("\\Seen",))
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


async def _smtp_send_oauth(
    *,
    host: str,
    port: int,
    starttls: bool,
    email: str,
    access_token: str,
    msg: object,
    recipients: list[str],
) -> None:
    """Send a built MIME message via SMTP XOAUTH2 (ADR-0025 §4, TD-030).

    ``aiosmtplib`` 3.0.2 has no XOAUTH2 mechanism, so we drive the raw
    ``AUTH XOAUTH2 <base64>`` command after STARTTLS, then ``send_message``.
    """
    client = aiosmtplib.SMTP(
        hostname=host,
        port=port,
        use_tls=False,
        start_tls=False,
        timeout=_SMTP_TIMEOUT,
    )
    auth_b64 = build_xoauth2_string(email, access_token)
    try:
        await client.connect()
        if starttls:
            await client.starttls(tls_context=_ssl_context())
        await client.ehlo()
        resp = await client.execute_command(b"AUTH", b"XOAUTH2", auth_b64.encode("ascii"))
        if resp.code != 235:
            raise SMTPSendFailedError(
                "SMTP XOAUTH2 authentication failed",
                details={"detail": f"smtp_code_{resp.code}"},
            )
        await client.send_message(msg, recipients=recipients)  # type: ignore[arg-type]
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
    finally:
        with contextlib.suppress(Exception):
            await client.quit()


async def smtp_send_message(
    account: MailAccount,
    msg: EmailMessage,
    recipients: list[str],
    *,
    session: AsyncSession,
) -> None:
    """Shared SMTP send core for ``send`` and ``forward`` (ADR-0034 §5).

    Encapsulates both authentication branches:

    - **password:** decrypt ``smtp_encrypted_password`` (falling back to
      ``encrypted_password``) and SMTP LOGIN;
    - **oauth_outlook:** fetch a fresh XOAUTH2 access token
      (:class:`OutlookTokenService`) and drive ``AUTH XOAUTH2``.

    Plus ``assert_public_host`` (SSRF re-check at send time), the shared TLS
    context and the fail-fast ``_SMTP_TIMEOUT``. Does **not** append to the
    "Sent" folder — the caller decides (``send`` does a best-effort append;
    ``forward`` does not, ADR-0034 §5). Raises :class:`SMTPSendFailedError`
    on any SMTP/transport failure and :class:`OAuthReconsentRequiredError`
    when an oauth account needs re-consent.
    """
    assert_public_host(account.smtp_host, port=account.smtp_port)

    if account.auth_type == "oauth_outlook":
        if account.oauth_needs_consent:
            raise OAuthReconsentRequiredError("Reconnect Outlook to send from this account")
        access_token = await OutlookTokenService(session).get_valid_access_token(account)
        await _smtp_send_oauth(
            host=account.smtp_host,
            port=account.smtp_port,
            starttls=account.smtp_starttls,
            email=account.email,
            access_token=access_token,
            msg=msg,
            recipients=recipients,
        )
        return

    # Password path — prefer the dedicated SMTP password, fall back to the
    # IMAP password (same precedence as the pre-ADR-0034 inline send).
    if account.smtp_encrypted_password is not None:
        smtp_pwd = decrypt_mail_password(account.smtp_encrypted_password, account.id)
    else:
        assert account.encrypted_password is not None
        smtp_pwd = decrypt_mail_password(account.encrypted_password, account.id)
    smtp_user = account.smtp_username or account.email
    try:
        await aiosmtplib.send(
            msg,
            hostname=account.smtp_host,
            port=account.smtp_port,
            username=smtp_user,
            password=smtp_pwd,
            use_tls=account.smtp_ssl,
            start_tls=account.smtp_starttls,
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
            group_ids=scope.group_ids, owner_user_id=scope.user_id
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

        # 3. Resolve the OAuth access token if needed. It is consumed by the
        #    SMTP send (inside ``smtp_send_message``) AND the best-effort IMAP
        #    append below; password accounts decrypt lazily in each path
        #    (ADR-0025 / ADR-0034 §5).
        is_oauth = acc.auth_type == "oauth_outlook"
        access_token: str | None = None
        if is_oauth:
            if acc.oauth_needs_consent:
                raise OAuthReconsentRequiredError("Reconnect Outlook to send from this account")
            access_token = await OutlookTokenService(self._db).get_valid_access_token(acc)

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

        # 5. SMTP send via the shared helper (XOAUTH2 for oauth accounts,
        #    LOGIN otherwise). ADR-0034 §5 — reused by the forward dispatcher.
        await smtp_send_message(acc, msg, recipients, session=self._db)

        # 6. Best-effort IMAP APPEND to the Sent folder.
        appended = False
        appended_error: str | None = None
        try:
            assert_public_host(acc.imap_host, port=acc.imap_port)
            if is_oauth:
                assert access_token is not None
                await asyncio.wait_for(
                    asyncio.to_thread(
                        _imap_append_oauth_blocking,
                        host=acc.imap_host,
                        port=acc.imap_port,
                        email=acc.email,
                        access_token=access_token,
                        raw_message_bytes=bytes(msg),
                    ),
                    timeout=_IMAP_APPEND_TIMEOUT + 5,
                )
            else:
                assert acc.encrypted_password is not None
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
