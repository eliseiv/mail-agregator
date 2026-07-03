"""APScheduler job: drain the mail-forwarding dispatch queue (ADR-0034 §3.3).

Tick cadence: ``settings.FORWARD_DISPATCH_INTERVAL_SECONDS`` (default 5s).
``max_instances=1`` + ``coalesce`` + registered only when
``FORWARDING_ENABLED`` — see ``worker/app/main.py``.

Per tick:

1. ``LPOP forward_dispatch_queue count=FORWARD_BATCH_SIZE`` — drain up to N.
2. For each item, :func:`_dispatch_one` resolves the message + mailbox +
   forwarding config, claims a ``message_forwards`` row (idempotency), builds
   the forward MIME (streaming attachments from MinIO) and sends it with the
   mailbox's own SMTP credentials.
3. Any failure inside the per-item call is caught + logged; the dispatcher
   keeps running so subsequent items are still processed.

Fire-and-forget after claim: **no** retry, **no** recovery scan (ADR-0034
§3.6, TD-043). Exactly-once is delegated to the ``message_forwards`` UNIQUE
constraint; an SMTP failure is recorded on the claim row (``error``) and never
retried. The message itself is always available in the UI.
"""

from __future__ import annotations

import datetime as _dt
from collections.abc import Awaitable
from typing import Any, cast

from sqlalchemy.ext.asyncio import AsyncSession

from shared.config import get_settings
from shared.db import make_session
from shared.logging import get_logger
from shared.redis_client import get_redis
from shared.storage import Storage, get_storage

log = get_logger(__name__)

_QUEUE_KEY = "forward_dispatch_queue"


async def forward_dispatch() -> None:
    """One forward-dispatcher tick."""
    settings = get_settings()
    redis = get_redis()
    batch_size = settings.FORWARD_BATCH_SIZE

    raw_items = await cast(
        Awaitable[bytes | str | list[Any] | None],
        redis.lpop(_QUEUE_KEY, count=batch_size),
    )
    if not raw_items:
        return

    if isinstance(raw_items, bytes | str):
        items: list[str] = [raw_items.decode() if isinstance(raw_items, bytes) else raw_items]
    else:
        items = [(it.decode() if isinstance(it, bytes) else it) for it in raw_items]

    log.info("forward_dispatch_start", batch=len(items))

    tally: dict[str, int] = {}
    storage = get_storage()
    # Local import to avoid pulling the backend.app heavy graph at module load.
    from backend.app.forwarding.dispatch_service import _QueuePayload

    for raw in items:
        payload = _QueuePayload.from_json(raw)
        if payload is None:
            log.warning("forward_dispatch_malformed", raw_excerpt=raw[:200])
            tally["malformed"] = tally.get("malformed", 0) + 1
            continue
        try:
            async with make_session() as s, s.begin():
                outcome = await _dispatch_one(s, payload.message_id, storage)
        except Exception as exc:
            # Never propagate — subsequent items must still be processed.
            log.warning(
                "forward_dispatch_item_error",
                message_id=payload.message_id,
                detail=str(exc)[:200],
                error_type=type(exc).__name__,
            )
            outcome = "error"
        tally[outcome] = tally.get(outcome, 0) + 1

    log.info(
        "forward_dispatch_finish",
        sent=tally.get("sent", 0),
        errors=tally.get("error", 0),
        skipped_dedup=tally.get("skip_dedup", 0),
        skipped_no_config=tally.get("skip_no_config", 0),
        skipped_loop=tally.get("skip_loop", 0),
        skipped_history=tally.get("skip_history", 0),
        skipped_personal=tally.get("skip_personal", 0),
        skipped_rate_limited=tally.get("skip_rate_limited", 0),
        skipped_missing=tally.get("skip_missing", 0),
    )


def _as_aware(value: _dt.datetime) -> _dt.datetime:
    """Coerce a possibly-naive datetime to UTC-aware (defensive)."""
    if value.tzinfo is None:
        return value.replace(tzinfo=_dt.UTC)
    return value


def _safe_forward_error(exc: BaseException, max_len: int = 500) -> str:
    """Single-line, length-clamped error text WITHOUT host/stack detail.

    Only the exception type + its (already provider-facing) message are kept;
    ``docs/06-security.md`` §1.14 forbids leaking SMTP-host / traceback here.
    """
    detail = str(exc).replace("\r", " ").replace("\n", " ")
    return f"{type(exc).__name__}: {detail}"[:max_len]


async def _read_object(storage: Storage, key: str) -> bytes:
    """Fully read a MinIO object by streaming it in chunks."""
    chunks: list[bytes] = []
    async for chunk in storage.get_object_stream(key):
        chunks.append(chunk)
    return b"".join(chunks)


async def _resolve_attachment_parts(
    storage: Storage,
    attachments: list[Any],
    *,
    max_total_bytes: int,
) -> list[Any]:
    """Resolve ORM attachments into :class:`ForwardAttachmentPart` list.

    ``skipped_too_large`` attachments and any attachment that would push the
    running total past ``max_total_bytes`` are represented with ``data=None``
    (their filename is listed in the forward body). Included attachments carry
    the bytes streamed from MinIO.
    """
    from backend.app.send.mime import ForwardAttachmentPart

    parts: list[Any] = []
    running = 0
    for att in attachments:
        if att.skipped_too_large:
            parts.append(ForwardAttachmentPart(att.filename, att.content_type, None))
            continue
        if running + int(att.size_bytes or 0) > max_total_bytes:
            parts.append(ForwardAttachmentPart(att.filename, att.content_type, None))
            continue
        data = await _read_object(storage, att.s3_key)
        running += len(data)
        parts.append(ForwardAttachmentPart(att.filename, att.content_type, data))
    return parts


async def _dispatch_one(  # noqa: PLR0911 — flat early-return guards are clearer than nesting
    session: AsyncSession, message_id: int, storage: Storage
) -> str:
    """Process one forward (ADR-0034 §3.4). Returns an outcome label."""
    from backend.app.repositories.mail_accounts import MailAccountsRepo
    from backend.app.repositories.messages import MessagesRepo
    from shared.models import Message

    settings = get_settings()

    # 1. Load message + account + attachments.
    message = await session.get(Message, message_id)
    if message is None:
        log.info("forward_dispatch_message_missing", message_id=message_id)
        return "skip_missing"
    account = await MailAccountsRepo(session).get_by_id(message.mail_account_id)
    if account is None:
        log.info(
            "forward_dispatch_account_missing",
            message_id=message_id,
            mail_account_id=message.mail_account_id,
        )
        return "skip_missing"

    # 2. Personal mailbox — never forwarded (defensive; enqueue already filters).
    if account.group_id is None:
        return "skip_personal"

    # 3. Resolve the (current) team's forwarding config.
    from backend.app.repositories.group_forwarding import GroupForwardingRepo

    gf = await GroupForwardingRepo(session).get_by_group_id(account.group_id)
    if gf is None or not gf.is_active:
        return "skip_no_config"

    # 4. Temporal-guard — don't flood history / initial-backfill.
    if _as_aware(message.internal_date) < _as_aware(gf.created_at):
        return "skip_history"

    # 5. Loop-guard (part 2) — forwarding to the mailbox's own address.
    if gf.forward_to == account.email:
        return "skip_loop"

    # 6. Claim (exactly-once). Conflict → already forwarded for this team.
    from backend.app.repositories.message_forwards import MessageForwardsRepo

    forwards_repo = MessageForwardsRepo(session)
    fid = await forwards_repo.try_reserve(
        message_id=message_id, group_id=account.group_id, forward_to=gf.forward_to
    )
    if fid is None:
        return "skip_dedup"

    # 7. Per-account forward throttle (ADR-0034 §6). Consumed AFTER the claim
    #    (§14.4 step 7); on exceed we record it on the claim row so no orphan
    #    (sent_at NULL / error NULL) row is left behind. fail-open on empty key.
    from backend.app.rate_limit import LIMIT_FORWARD_PER_ACCOUNT, Limit, try_consume

    runtime_limit = Limit(
        name=LIMIT_FORWARD_PER_ACCOUNT.name,
        capacity=settings.FORWARD_PER_ACCOUNT_PER_MINUTE,
        window_seconds=LIMIT_FORWARD_PER_ACCOUNT.window_seconds,
    )
    if not await try_consume(runtime_limit, str(account.id)):
        log.warning(
            "forward_rate_limited",
            message_id=message_id,
            mail_account_id=account.id,
        )
        await forwards_repo.mark_error(fid, "rate_limited: per-account forward throttle exceeded")
        return "skip_rate_limited"

    # 8-9. Build the forward MIME (streaming attachments from MinIO) and send
    #      it with the mailbox's own SMTP credentials. Both the MIME/stream
    #      build and the send are wrapped so a MinIO failure records an error
    #      instead of leaving an orphan claim (reviewer note).
    from backend.app.send.mime import build_forward_mime
    from backend.app.send.service import smtp_send_message

    attachments = (await MessagesRepo(session).list_attachments_bulk([message_id])).get(
        message_id, []
    )
    try:
        parts = await _resolve_attachment_parts(
            storage, attachments, max_total_bytes=settings.FORWARD_MAX_TOTAL_BYTES
        )
        msg = build_forward_mime(
            account_email=account.email,
            forward_to=gf.forward_to,
            message=message,
            attachment_parts=parts,
        )
        # ADR-0034 §5: no Sent-append for forwards.
        await smtp_send_message(account, msg, [gf.forward_to], session=session)
    except Exception as exc:
        safe = _safe_forward_error(exc)
        await forwards_repo.mark_error(fid, safe)
        log.warning(
            "forward_dispatch_send_failed",
            message_id=message_id,
            mail_account_id=account.id,
            group_id=account.group_id,
            detail=safe,
        )
        return "error"

    await forwards_repo.mark_sent(fid)
    log.info(
        "forward_dispatch_sent",
        message_id=message_id,
        mail_account_id=account.id,
        group_id=account.group_id,
    )
    return "sent"
