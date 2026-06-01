"""Sync IMAP fetcher for the worker.

Wraps ``imap-tools`` so we expose a single :class:`FetchedMessage` shape to
``sync_cycle``. The IMAP library is sync; callers wrap in
:func:`asyncio.to_thread`.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import errno
import imaplib
import socket
import time
from collections.abc import Iterator
from dataclasses import dataclass

import imap_tools
from imap_tools import AND, MailBoxUnencrypted

from shared.config import get_settings
from shared.html_sanitize import sanitize_email_html, strip_invisible_padding
from shared.logging import get_logger

log = get_logger(__name__)

# ADR-0026 §4: backoff schedule (seconds) for connection/login retries. The
# Nth retry sleeps ``_RETRY_BACKOFFS[N-1]``; if more retries than entries are
# configured the last value is reused. Third element (2.0) added in the
# ADR-0026 update so the default 3 retries cover the sporadic Microsoft
# "authenticated but not connected" flake (0.5/1.0/2.0).
_RETRY_BACKOFFS: tuple[float, ...] = (0.5, 1.0, 2.0)

# Networking ``OSError`` errnos that represent an immediate connect failure
# worth retrying (NOT timeouts). Mirrors ADR-0026 §4.
_RETRYABLE_ERRNOS: frozenset[int] = frozenset(
    {
        errno.ECONNREFUSED,
        errno.ECONNRESET,
        errno.EHOSTUNREACH,
        errno.ENETUNREACH,
    }
)

# ADR-0026 update §4: sporadic / transient IMAP server responses that warrant
# an in-cycle retry. Microsoft personal Outlook IMAP intermittently answers a
# perfectly valid XOAUTH2 on a healthy mailbox with
# ``imaplib.IMAP4.error: User is authenticated but not connected``; a 2nd/3rd
# attempt with backoff clears it. We also retry generic provider "try again" /
# "temporarily" / "too many" rate-limit flakes (lower-case substring match on
# the IMAP4 error text). These are matched ONLY for ``imaplib.IMAP4.error`` /
# ``imaplib.IMAP4.abort`` instances (raised by ``mailbox.xoauth2`` /
# ``mailbox.login`` inside imap-tools).
_RETRYABLE_IMAP_SUBSTRINGS: tuple[str, ...] = (
    "authenticated but not connected",
    "not connected",
    "try again",
    "temporarily",
    "too many",
)

# Permanent auth markers that MUST NOT be retried even when they arrive as an
# ``IMAP4.error`` — a wrong password / disabled account is not a flake. Checked
# FIRST in :func:`_is_retryable_imap_error` so e.g. "AUTHENTICATIONFAILED" never
# matches the broad "not connected" family. (``"authenticated but not
# connected"`` contains "authenticated" but is NOT one of these — order +
# specificity keep them apart.)
_PERMANENT_IMAP_SUBSTRINGS: tuple[str, ...] = (
    "authenticationfailed",
    "login failed",
    "invalid credentials",
    "account is disabled",
    "account has been blocked",
)


def _is_retryable_connect_error(exc: BaseException) -> bool:
    """True for immediate DNS/connect failures we retry (ADR-0026 sec. 4).

    Explicitly EXCLUDES ``socket.timeout`` / ``TimeoutError``: retrying a
    timeout would multiply the wait ((retries+1)x timeout) and blow the cycle
    budget. Auth/permanent failures are also never retried (they surface as
    ``imap_tools`` login errors, not the network types below).
    """
    if isinstance(exc, socket.timeout | TimeoutError):
        return False
    if isinstance(exc, socket.gaierror | ConnectionError):
        return True
    if isinstance(exc, OSError):
        return exc.errno in _RETRYABLE_ERRNOS
    return False


def _is_retryable_imap_error(exc: BaseException) -> bool:
    """True for SPORADIC transient IMAP errors worth an in-cycle retry.

    Only ``imaplib.IMAP4.error`` / ``imaplib.IMAP4.abort`` instances qualify
    (these wrap an IMAP ``NO``/``BAD`` reply or a dropped server connection).
    Real auth failures (``AUTHENTICATIONFAILED`` / ``LOGIN failed`` / invalid
    credentials / disabled account) are explicitly EXCLUDED — they are
    permanent and must propagate so the classifier disables the account; a
    retry there only wastes the cycle budget.

    The canonical case (ADR-0026 update) is Microsoft personal Outlook IMAP
    returning ``User is authenticated but not connected`` on a healthy mailbox:
    it is transient (the next attempt succeeds), so we retry it.
    """
    if not isinstance(exc, imaplib.IMAP4.error | imaplib.IMAP4.abort):
        return False
    text = str(exc).lower()
    # Permanent auth/account-state markers win — never retry these.
    if any(needle in text for needle in _PERMANENT_IMAP_SUBSTRINGS):
        return False
    return any(needle in text for needle in _RETRYABLE_IMAP_SUBSTRINGS)


@dataclass(slots=True)
class FetchedAttachment:
    filename: str
    content_type: str | None
    size_bytes: int
    payload: bytes  # empty if oversized


@dataclass(slots=True)
class FetchedMessage:
    uid: int
    message_id_header: str | None
    from_addr: str
    from_name: str | None
    to_addrs: str
    cc_addrs: str | None
    subject: str | None
    internal_date: _dt.datetime
    body_text: str
    # Round-12 bug B: sanitised HTML body (``bleach.clean`` whitelist).
    # ``None`` when the source email had no ``text/html`` part.
    body_html: str | None
    body_truncated: bool
    body_present: bool
    in_reply_to: str | None
    refs_header: str | None
    attachments: list[FetchedAttachment]


@dataclass(slots=True)
class FetchedBox:
    uidvalidity: int
    uidnext: int | None
    new_messages: list[FetchedMessage]


def _truncate_body(body: str, max_bytes: int) -> tuple[str, bool]:
    """Truncate ``body`` to ``max_bytes`` UTF-8 bytes. Returns (text, truncated)."""
    encoded = body.encode("utf-8")
    if len(encoded) <= max_bytes:
        return body, False
    cut = encoded[:max_bytes]
    # Don't split a multi-byte character.
    return cut.decode("utf-8", errors="ignore"), True


def _from_imap_msg(
    msg: imap_tools.MailMessage,
    *,
    max_body_bytes: int,
    max_att_bytes: int,
) -> FetchedMessage:
    """Extract our internal shape from one ``imap-tools`` message."""
    # Body: prefer text/plain, else html2text(html).
    text = msg.text or ""
    body_present = True
    if not text and msg.html:
        from html2text import HTML2Text

        h = HTML2Text()
        h.body_width = 0
        h.ignore_images = True
        h.ignore_links = False
        text = h.handle(msg.html)
    if not text and not msg.html:
        body_present = False
        text = ""

    # Strip the zero-width / invisible padding mass-mail engines insert
    # into the plain-text part — they survive html2text and bloat both
    # ``body_text`` and Telegram messages.
    text = strip_invisible_padding(text)

    text_clamped, truncated = _truncate_body(text, max_body_bytes)

    # Round-12 bug B: keep the sanitised HTML body when the source has
    # a ``text/html`` part — the inbox renders it for clickable links and
    # tables, and the Telegram callback path converts it on the fly to
    # the Bot-API HTML subset. We apply the same byte budget as the
    # plain-text path so a 50 MB marketing email cannot blow up the DB
    # row.
    body_html: str | None = None
    if msg.html:
        sanitised = sanitize_email_html(msg.html)
        if sanitised:
            html_clamped, _html_truncated = _truncate_body(sanitised, max_body_bytes)
            body_html = html_clamped

    # Attachments — skip oversized.
    atts: list[FetchedAttachment] = []
    for att in msg.attachments:
        size = len(att.payload) if att.payload else 0
        if size > max_att_bytes:
            atts.append(
                FetchedAttachment(
                    filename=att.filename or "attachment",
                    content_type=att.content_type,
                    size_bytes=size,
                    payload=b"",
                )
            )
        else:
            atts.append(
                FetchedAttachment(
                    filename=att.filename or "attachment",
                    content_type=att.content_type,
                    size_bytes=size,
                    payload=att.payload or b"",
                )
            )

    # internal_date: imap-tools provides UTC naive datetimes; coerce to aware.
    idate = msg.date
    if idate is None:
        idate = _dt.datetime.now(_dt.UTC)
    elif idate.tzinfo is None:
        idate = idate.replace(tzinfo=_dt.UTC)

    from_addr = msg.from_ or ""
    from_name: str | None = None
    if msg.from_values:
        from_addr = msg.from_values.email or from_addr
        from_name = msg.from_values.name or None

    return FetchedMessage(
        uid=int(msg.uid) if msg.uid else 0,
        message_id_header=msg.headers.get("message-id", (None,))[0] if msg.headers else None,
        from_addr=from_addr,
        from_name=from_name,
        to_addrs=", ".join(msg.to) if msg.to else "",
        cc_addrs=", ".join(msg.cc) if msg.cc else None,
        subject=msg.subject or None,
        internal_date=idate,
        body_text=text_clamped,
        body_html=body_html,
        # Truncation flag is independent of source format (text vs html2text).
        body_truncated=truncated,
        body_present=body_present,
        in_reply_to=msg.headers.get("in-reply-to", (None,))[0] if msg.headers else None,
        refs_header=msg.headers.get("references", (None,))[0] if msg.headers else None,
        attachments=atts,
    )


def _open_mailbox(*, host: str, port: int, ssl_on: bool, timeout: int) -> imap_tools.BaseMailBox:
    if ssl_on:
        return imap_tools.MailBox(host, port=port, timeout=timeout)
    return MailBoxUnencrypted(host, port=port, timeout=timeout)


def _connect_and_login(
    *,
    host: str,
    port: int,
    ssl_on: bool,
    username: str,
    password: str | None,
    access_token: str | None,
    timeout: int,
) -> imap_tools.BaseMailBox:
    """Open the connection + authenticate, retrying transient DNS/connect AND
    sporadic transient IMAP errors (ADR-0026 §4 + update).

    ``SYNC_CONNECT_RETRIES`` (default 3) extra attempts with backoff
    0.5s/1.0s/2.0s on:

    * ``gaierror`` / ``ConnectionError`` / networking ``OSError`` (DNS/connect),
      via :func:`_is_retryable_connect_error`; and
    * sporadic ``imaplib.IMAP4.error`` / ``IMAP4.abort`` whose text is a known
      transient flake (e.g. Microsoft "User is authenticated but not
      connected"), via :func:`_is_retryable_imap_error`.

    Timeouts and real auth/permanent errors (``AUTHENTICATIONFAILED`` / invalid
    credentials / disabled account) propagate immediately (no retry). On success
    the open, authenticated mailbox is returned (caller owns logout).
    """
    retries = get_settings().SYNC_CONNECT_RETRIES
    attempt = 0
    while True:
        mailbox = _open_mailbox(host=host, port=port, ssl_on=ssl_on, timeout=timeout)
        try:
            if access_token is not None:
                # XOAUTH2 path (oauth_outlook accounts).
                mailbox.xoauth2(username, access_token, initial_folder="INBOX")
            else:
                assert password is not None, "fetch_blocking needs a password or an access_token"
                mailbox.login(username, password, initial_folder="INBOX")
            return mailbox
        except BaseException as exc:
            # Best-effort close the half-open socket before retrying.
            with contextlib.suppress(Exception):
                mailbox.logout()
            retryable = _is_retryable_connect_error(exc) or _is_retryable_imap_error(exc)
            if attempt >= retries or not retryable:
                raise
            backoff = _RETRY_BACKOFFS[min(attempt, len(_RETRY_BACKOFFS) - 1)]
            log.warning(
                "imap_connect_retry",
                host=host,
                port=port,
                attempt=attempt + 1,
                max_retries=retries,
                backoff_seconds=backoff,
                detail=f"{type(exc).__name__}: {exc}"[:200],
            )
            time.sleep(backoff)
            attempt += 1


def fetch_blocking(
    *,
    host: str,
    port: int,
    ssl_on: bool,
    username: str,
    password: str | None = None,
    access_token: str | None = None,
    last_synced_uidnext: int | None,
    last_uidvalidity: int | None,
    initial_sync_days: int,
    max_body_bytes: int,
    max_att_bytes: int,
    timeout: int,
) -> FetchedBox:
    """Sync IMAP fetch. Returns the new messages plus updated UID metadata.

    Implements ADR-0008 (UIDNEXT-based incremental + UIDVALIDITY check +
    initial 30-day backfill).

    Authentication (ADR-0025 §4): when ``access_token`` is provided, the
    mailbox authenticates via SASL XOAUTH2 (``MailBox.xoauth2`` — first-class
    in imap-tools 1.6, TD-030); otherwise it does the classic ``LOGIN`` with
    ``password``. Exactly one of ``password`` / ``access_token`` must be set.
    """
    # ADR-0026 §4: open + authenticate with DNS/connect retry. ``outlook.
    # office365.com`` is always SSL; ``_open_mailbox`` honours ``ssl_on``
    # regardless. XOAUTH2 vs LOGIN is chosen inside the helper.
    mailbox = _connect_and_login(
        host=host,
        port=port,
        ssl_on=ssl_on,
        username=username,
        password=password,
        access_token=access_token,
        timeout=timeout,
    )
    try:
        # imap-tools returns these as either str or int depending on the
        # server reply; coerce defensively.
        status_uidv = mailbox.folder.status("INBOX", ["UIDVALIDITY"]).get("UIDVALIDITY")
        uidvalidity = int(status_uidv) if status_uidv is not None else 0
        status_uidnext = mailbox.folder.status("INBOX", ["UIDNEXT"]).get("UIDNEXT")
        uidnext: int | None = int(status_uidnext) if status_uidnext is not None else None

        # Decide initial vs incremental.
        do_initial = (
            last_synced_uidnext is None
            or last_uidvalidity is None
            or last_uidvalidity != uidvalidity
        )

        if do_initial:
            since = _dt.datetime.now(_dt.UTC).date() - _dt.timedelta(days=initial_sync_days)
            criteria = AND(date_gte=since)
            uids = list(mailbox.uids(criteria))
        else:
            assert last_synced_uidnext is not None
            try:
                raw_uids = list(mailbox.uids(f"UID {last_synced_uidnext}:*"))
            except imap_tools.errors.MailboxFetchError:
                raw_uids = []
            uids = [u for u in raw_uids if int(u) >= last_synced_uidnext]

        new_messages: list[FetchedMessage] = []
        # Batch by 50 UIDs.
        batch_size = 50
        for batched_uids in _chunks(uids, batch_size):
            for msg in _fetch_iter(mailbox, batched_uids):
                fetched = _from_imap_msg(
                    msg,
                    max_body_bytes=max_body_bytes,
                    max_att_bytes=max_att_bytes,
                )
                new_messages.append(fetched)

        # Compute final uidnext per ADR-0008 step 8.
        if uidnext is None and new_messages:
            uidnext = max(m.uid for m in new_messages) + 1
        if uidnext is None:
            uidnext = last_synced_uidnext  # untouched

        return FetchedBox(
            uidvalidity=uidvalidity,
            uidnext=uidnext,
            new_messages=new_messages,
        )
    finally:
        with contextlib.suppress(Exception):
            mailbox.logout()


def _fetch_iter(
    mailbox: imap_tools.BaseMailBox, uids: list[str]
) -> Iterator[imap_tools.MailMessage]:
    if not uids:
        return iter([])
    result: Iterator[imap_tools.MailMessage] = mailbox.fetch(
        AND(uid=",".join(uids)),
        mark_seen=False,
        bulk=True,
    )
    return result


def _chunks(seq: list[str], size: int) -> Iterator[list[str]]:
    for i in range(0, len(seq), size):
        yield seq[i : i + size]
