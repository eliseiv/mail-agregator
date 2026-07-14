"""SendService — SMTP send, best-effort IMAP append (+ legacy reply persist).

ADR-0044 §4 (phase A3): the session ``send`` (HTML form, visibility scope) and
the forward relay (``smtp_send_via_relay``, ADR-0034) went away with the UI and
forwarding.

Two send paths co-exist (ADR-0048 §3 — phase A2.1 is ADDITIVE):

- :meth:`SendService.send_from_mailbox` — the generic send behind
  ``POST /api/external/mailboxes/{id}/send`` (ADR-0048 §1): transport only, **no**
  ``sent_messages`` write (the durable record lives in the CRM).
- :meth:`SendService.send_external_reply` — the legacy message-scoped reply
  (ADR-0035), still persisting ``sent_messages``. It is removed together with
  that writer in phase A2.2, once the CRM is confirmed on the generic send in
  production.

Both share one transport (:meth:`SendService._send_transport`) — MIME/SMTP/append
are not duplicated.
"""

from __future__ import annotations

import asyncio
import contextlib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage

import aiosmtplib
import imap_tools
from imap_tools import MailBoxUnencrypted
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.accounts.testers import build_xoauth2_string
from backend.app.exceptions import (
    NotFoundError,
    OAuthReconsentRequiredError,
    SMTPSendFailedError,
)
from backend.app.oauth.service import OutlookTokenService
from backend.app.repositories.mail_accounts import MailAccountsRepo
from backend.app.repositories.messages import MessagesRepo
from backend.app.repositories.sent_messages import SentMessagesRepo
from backend.app.security import assert_public_host_async
from backend.app.send.mime import build_mime, generate_message_id
from backend.app.send.schemas import SendMessageResponse
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


@dataclass(frozen=True, slots=True)
class _TransportResult:
    """What the transport leg produced: the wire ``Message-ID`` + append outcome.

    Returned by :meth:`SendService._send_transport` (MIME → SMTP → best-effort
    IMAP "Sent" append). The generic send (ADR-0048 §1) consumes only
    ``smtp_message_id``; the legacy reply additionally persists ``appended`` /
    ``appended_error`` into ``sent_messages``.
    """

    smtp_message_id: str
    appended: bool
    appended_error: str | None


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


class SendService:
    def __init__(self, session: AsyncSession) -> None:
        self._db = session
        self._accounts = MailAccountsRepo(session)
        self._messages = MessagesRepo(session)
        self._sent = SentMessagesRepo(session)

    @staticmethod
    def _resolve_threading(original: Message) -> tuple[str | None, str | None]:
        """Build ``In-Reply-To`` / ``References`` headers from ``original``.

        Used by the external reply (ADR-0035): a new outgoing message is
        threaded onto an existing one. Returns ``(in_reply_to_header,
        references_header)`` — both ``None``
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

    async def send_from_mailbox(
        self,
        *,
        mail_account_id: int,
        to: list[str],
        cc: list[str] | None,
        subject: str | None,
        body_text: str,
        in_reply_to: str | None,
        refs: str | None,
    ) -> str:
        """Generic send from mailbox ``{id}`` (ADR-0048 §1, phase A2.1).

        The CRM owns the message store, the reply defaults and the threading
        headers; the aggregator is a thin SMTP executor: resolve the mailbox →
        reuse the transport (MIME → SMTP → best-effort IMAP "Sent" append) →
        return the ``Message-ID`` it put on the wire.

        **No ``sent_messages`` write** (ADR-0048 §1/§3 A2.1): the durable record
        of the send lives in the CRM (``mail_sent_messages``), so this path never
        touches :class:`SentMessagesRepo` — the writer used by
        :meth:`send_external_reply` is untouched and still in place (A2.1 is
        additive; the old path is removed in A2.2).

        Threading headers are written **exactly as passed** — the aggregator does
        not synthesise them (ADR-0048 §1).

        Raises :class:`NotFoundError` (404) when mailbox ``{id}`` does not exist
        (ADR-0048 §4: a 404 here means "no such MAILBOX" — there is no message in
        this contract at all). :class:`OAuthReconsentRequiredError` (409) and
        :class:`SMTPSendFailedError` (502) propagate from the transport.
        """
        # Resolve by id across all mailboxes — the same reach the rest of the
        # external WRITE section already has (PATCH / DELETE / sync resolve an
        # arbitrary ``mail_accounts.id`` under the synthetic ``crm-service``
        # super_admin scope, ``external/write_service.py``). The canonical-dedup
        # of the READ/reply paths is a disclosure guard for the message scope and
        # has no counterpart here: the mailbox id comes from the CRM's own
        # catalogue, and the aggregator must be able to send from any mailbox it
        # holds credentials for.
        account = await self._accounts.get_by_id(mail_account_id)
        if account is None:
            raise NotFoundError("Mailbox not found")

        result = await self._send_transport(
            account=account,
            to=to,
            cc=cc,
            bcc=None,
            subject=subject,
            body=body_text,
            in_reply_header=in_reply_to,
            refs_header=refs,
        )
        return result.smtp_message_id

    async def _send_transport(
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
    ) -> _TransportResult:
        """Transport-only send core: OAuth resolve → MIME → SMTP → IMAP append.

        Everything :meth:`_send_core` used to do EXCEPT the ``sent_messages``
        persist — extracted so the generic send (ADR-0048 §1) can reuse the exact
        same MIME/SMTP/append pipeline **without** a local write, while the
        surviving reply path (:meth:`send_external_reply`) keeps persisting via
        :meth:`_send_core` (ADR-0048 §3: A2.1 is additive — the old writer stays
        until A2.2).
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

        return _TransportResult(
            smtp_message_id=message_id,
            appended=appended,
            appended_error=appended_error,
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
        """Transport + ``sent_messages`` persist (ADR-0035 §Migration step 6).

        The path of the surviving external reply. Untouched by ADR-0048 §3 (A2.1
        is additive): the ``INSERT`` below is still the reply's durable record;
        the generic send does NOT come through here.
        """
        transport = await self._send_transport(
            account=account,
            to=to,
            cc=cc,
            bcc=bcc,
            subject=subject,
            body=body,
            in_reply_header=in_reply_header,
            refs_header=refs_header,
        )
        message_id = transport.smtp_message_id
        appended = transport.appended
        appended_error = transport.appended_error

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
