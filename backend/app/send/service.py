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
from backend.app.security import assert_public_host_async
from backend.app.send.mime import build_mime, generate_message_id
from backend.app.send.schemas import SendMessageRequest, SendMessageResponse
from shared.config import get_settings
from shared.crypto import decrypt_mail_password
from shared.logging import get_logger
from shared.models import MailAccount, Message

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

    Plus ``assert_public_host_async`` (SSRF re-check at send time), the shared
    TLS context and the fail-fast ``_SMTP_TIMEOUT``. Does **not** append to the
    "Sent" folder — the caller decides (``send`` does a best-effort append;
    ``forward`` does not, ADR-0034 §5). Raises :class:`SMTPSendFailedError`
    on any SMTP/transport failure and :class:`OAuthReconsentRequiredError`
    when an oauth account needs re-consent.

    TD-056: the SSRF guard resolves OFF the event loop. Its blocking
    ``getaddrinfo`` used to run in the loop thread — a hung resolver stalled the
    WHOLE api container (and, via the forward dispatcher, the worker loop), and
    the phase timeouts of ``aiosmtplib`` never covered the resolve leg, so the
    declared send budget (``_SMTP_TIMEOUT + _IMAP_APPEND_TIMEOUT + 5 = 55 < 60``)
    was not guaranteed for it.
    """
    await assert_public_host_async(account.smtp_host, port=account.smtp_port)

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


async def smtp_send_via_relay(msg: EmailMessage, recipients: list[str]) -> None:
    """Send a built forward MIME through the service SMTP relay (ADR-0034 §5).

    Used by the forward dispatcher when ``settings.forward_relay_enabled`` —
    the forward leaves through the operator's relay (``FORWARD_SMTP_*``) instead
    of the receiving mailbox's own credentials, because many monitoring
    mailboxes cannot send (Gmail app-password revoked → BadCredentials, AOL
    drops the connection, Outlook OAuth lacks ``SMTP.Send``). The ``From`` is
    the relay's ``FORWARD_SMTP_FROM`` and ``Reply-To`` carries the original
    sender — both already set on ``msg`` by :func:`build_forward_mime`.

    Mirrors the password branch of :func:`smtp_send_message`: SSRF re-check
    (:func:`assert_public_host_async` on the relay host, off-loop — TD-056),
    shared TLS context,
    fail-fast ``_SMTP_TIMEOUT``, and the same error matrix
    (``SMTPException`` / ``TimeoutError`` / ``OSError`` →
    :class:`SMTPSendFailedError` with host detail stripped). No Sent-append
    (forwards never append, ADR-0034 §5).
    """
    settings = get_settings()
    await assert_public_host_async(settings.FORWARD_SMTP_HOST, port=settings.FORWARD_SMTP_PORT)
    try:
        await aiosmtplib.send(
            msg,
            hostname=settings.FORWARD_SMTP_HOST,
            port=settings.FORWARD_SMTP_PORT,
            username=settings.FORWARD_SMTP_USERNAME,
            password=settings.FORWARD_SMTP_PASSWORD,
            use_tls=settings.FORWARD_SMTP_SSL,
            start_tls=settings.FORWARD_SMTP_STARTTLS,
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

    @staticmethod
    def _resolve_threading(original: Message) -> tuple[str | None, str | None]:
        """Build ``In-Reply-To`` / ``References`` headers from ``original``.

        Shared by the session ``send`` (in-reply-to path) and the external
        reply (ADR-0035): both thread a new outgoing message onto an existing
        one. Returns ``(in_reply_to_header, references_header)`` — both ``None``
        when the original carries no ``Message-ID`` header (nothing to thread
        onto). ``References`` = original ``References`` + original ``Message-ID``
        per RFC 5322.
        """
        if not original.message_id_header:
            return None, None
        in_reply_header = original.message_id_header
        if original.refs_header:
            refs_header = original.refs_header.strip() + " " + original.message_id_header
        else:
            refs_header = original.message_id_header
        return in_reply_header, refs_header

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

        # 2. Resolve in-reply-to headers if requested (visibility-scoped).
        in_reply_header: str | None = None
        refs_header: str | None = None
        if payload.in_reply_to_message_id is not None:
            original = await self._messages.get_for_user_ids(
                message_id=payload.in_reply_to_message_id,
                mail_account_ids=visible,
            )
            if original is None:
                raise NotFoundError("Original message not found")
            in_reply_header, refs_header = self._resolve_threading(original)

        # 3-7. Shared send core (MIME → SMTP → IMAP-append → persist). The
        #      author is the caller (``scope.user_id``) — distinct from the
        #      mailbox owner so a leader sending from a member's mailbox is
        #      correctly attributed (ADR-0019 §7.3).
        return await self._send_core(
            account=acc,
            to=payload.to,
            cc=payload.cc,
            bcc=payload.bcc,
            subject=payload.subject,
            body=payload.body,
            in_reply_header=in_reply_header,
            refs_header=refs_header,
            author_user_id=scope.user_id,
        )

    async def send_external_reply(
        self,
        *,
        message_id: int,
        to: list[str] | None,
        cc: list[str] | None,
        subject: str | None,
        body: str,
    ) -> SendMessageResponse:
        """Reply to an existing message via the external API (ADR-0035).

        No user session: the message is resolved in the SAME canonical scope
        the pull API exposes (ADR-0029 §5), so the caller can only reply to a
        message it could have pulled. The sender is NOT chosen by the caller —
        it is fixed to the original message's mailbox (``from`` = the mailbox
        the message arrived at). Threading is derived server-side from the
        original. Reuses the shared send core (no MIME/SMTP duplication,
        ADR-0034 §5 / ADR-0035 §Decision).

        Raises :class:`NotFoundError` (404) when the message does not exist or
        is outside the canonical scope (non-canonical duplicate mailbox
        included — its existence is not disclosed, ADR-0035 §Edge cases).
        """
        # Resolve the original in the canonical-dedup scope (same as pull).
        canonical_ids = await self._accounts.list_canonical_account_ids()
        original = await self._messages.get_for_user_ids(
            message_id=message_id, mail_account_ids=canonical_ids
        )
        if original is None:
            raise NotFoundError("Original message not found")

        # Sender = the mailbox the original arrived at (never caller-chosen).
        from_account = await self._accounts.get_for_user_ids(
            canonical_ids, original.mail_account_id
        )
        if from_account is None:
            # Defensive: the account was resolved as canonical above, so this
            # only trips on a concurrent delete. Same opaque 404 as above.
            raise NotFoundError("Original message not found")

        # Server-derived defaults (NOT user input — bypass the request
        # validator, ADR-0035 §2): reply to the sender; "Re: " subject.
        reply_to = to or [original.from_addr]
        reply_subject = subject if subject is not None else "Re: " + (original.subject or "")
        in_reply_header, refs_header = self._resolve_threading(original)

        # Persist author = mailbox owner (external context has no session
        # author; FK-valid + semantically the owner sent the reply, ADR-0035 §7).
        return await self._send_core(
            account=from_account,
            to=reply_to,
            cc=cc,
            bcc=None,
            subject=reply_subject,
            body=body,
            in_reply_header=in_reply_header,
            refs_header=refs_header,
            author_user_id=from_account.user_id,
        )

    async def _send_core(
        self,
        *,
        account: MailAccount,
        to: list[str],
        cc: list[str] | None,
        bcc: list[str] | None,
        subject: str | None,
        body: str,
        in_reply_header: str | None,
        refs_header: str | None,
        author_user_id: int,
    ) -> SendMessageResponse:
        """Shared post-visibility send pipeline (ADR-0035 §Migration step 6).

        Steps: OAuth token resolve → MIME build → SMTP send → best-effort IMAP
        "Sent" append → persist ``sent_messages``. Reused verbatim by the
        session ``send`` and the external reply so MIME/SMTP/append/persist are
        NEVER duplicated. Callers own visibility + threading resolution and pass
        the resolved ``account`` / headers / ``author_user_id`` in.
        """
        # OAuth access token (consumed by the best-effort IMAP append below;
        # ``smtp_send_message`` resolves its own token for the SMTP leg).
        is_oauth = account.auth_type == "oauth_outlook"
        access_token: str | None = None
        if is_oauth:
            if account.oauth_needs_consent:
                raise OAuthReconsentRequiredError("Reconnect Outlook to send from this account")
            access_token = await OutlookTokenService(self._db).get_valid_access_token(account)

        message_id = generate_message_id()
        msg = build_mime(
            from_addr=account.email,
            to=to,
            cc=cc,
            bcc=bcc,
            subject=subject,
            body_text=body,
            in_reply_to_header=in_reply_header,
            references_header=refs_header,
            message_id=message_id,
        )

        recipients = list(to)
        if cc:
            recipients.extend(cc)
        if bcc:
            recipients.extend(bcc)

        # SMTP send via the shared helper (XOAUTH2 for oauth accounts, LOGIN
        # otherwise). ADR-0034 §5 — reused by the forward dispatcher too.
        await smtp_send_message(account, msg, recipients, session=self._db)

        # Best-effort IMAP APPEND to the Sent folder.
        appended = False
        appended_error: str | None = None
        try:
            # TD-056: off-loop SSRF re-check before the IMAP APPEND.
            await assert_public_host_async(account.imap_host, port=account.imap_port)
            if is_oauth:
                assert access_token is not None
                await asyncio.wait_for(
                    asyncio.to_thread(
                        _imap_append_oauth_blocking,
                        host=account.imap_host,
                        port=account.imap_port,
                        email=account.email,
                        access_token=access_token,
                        raw_message_bytes=bytes(msg),
                    ),
                    timeout=_IMAP_APPEND_TIMEOUT + 5,
                )
            else:
                assert account.encrypted_password is not None
                await asyncio.wait_for(
                    asyncio.to_thread(
                        _imap_append_blocking,
                        host=account.imap_host,
                        port=account.imap_port,
                        ssl_on=account.imap_ssl,
                        username=account.email,
                        password=decrypt_mail_password(account.encrypted_password, account.id),
                        raw_message_bytes=bytes(msg),
                    ),
                    timeout=_IMAP_APPEND_TIMEOUT + 5,
                )
            appended = True
        except Exception as exc:  # — best-effort
            appended_error = _safe_error_text(exc)
            log.info(
                "smtp_send_imap_append_failed",
                mail_account_id=account.id,
                detail=appended_error,
            )

        sent = await self._sent.insert(
            user_id=author_user_id,
            from_account_id=account.id,
            to_addrs=", ".join(to),
            cc_addrs=", ".join(cc) if cc else None,
            bcc_addrs=", ".join(bcc) if bcc else None,
            subject=subject,
            body_text=body,
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
