"""Sync IMAP fetcher for the worker.

Wraps ``imap-tools`` so we expose a single :class:`FetchedMessage` shape to
``sync_cycle``. The IMAP library is sync; callers wrap in
:func:`asyncio.to_thread`.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
from collections.abc import Iterator
from dataclasses import dataclass

import imap_tools
from imap_tools import AND, MailBoxUnencrypted

from shared.logging import get_logger

log = get_logger(__name__)


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

    text_clamped, truncated = _truncate_body(text, max_body_bytes)

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


def fetch_blocking(
    *,
    host: str,
    port: int,
    ssl_on: bool,
    username: str,
    password: str,
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
    """
    mailbox = _open_mailbox(host=host, port=port, ssl_on=ssl_on, timeout=timeout)
    mailbox.login(username, password, initial_folder="INBOX")
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
    return mailbox.fetch(
        AND(uid=",".join(uids)),
        mark_seen=False,
        bulk=True,
    )


def _chunks(seq: list[str], size: int) -> Iterator[list[str]]:
    for i in range(0, len(seq), size):
        yield seq[i : i + size]
