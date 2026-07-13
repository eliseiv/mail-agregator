"""CRM push connector — ingest + mailbox-status channels (ADR-0043 §2).

Both channels POST to the CRM with an HMAC-SHA256 signature over the **raw**
request body. The signature is computed over *exactly* the bytes that go out
on the wire — the JSON is serialised **once** and the resulting ``bytes`` are
both signed and sent (``content=raw_body``). A re-serialisation on the httpx
side (``json=...``) would change separators/ordering and break the signature,
so it is deliberately avoided.

Canonical signature form (byte-for-byte identical to CRM ``ADR-044`` §3 — an
f-string over ``bytes`` is FORBIDDEN, it yields the ``repr`` ``"b'...'"`` not
the bytes)::

    mac_input = str(timestamp).encode("ascii") + b"." + raw_body_bytes
    signature = hmac.new(secret.encode("utf-8"), mac_input, hashlib.sha256).hexdigest()

Wire headers: ``X-Mail-Signature: sha256=<hex>``, ``X-Mail-Timestamp: <unix>``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final, cast

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.repositories.mail_accounts import MailAccountsRepo
from backend.app.repositories.messages import MessagesRepo
from shared.config import get_settings
from shared.logging import get_logger
from shared.models import MailAccount, Message
from shared.redis_client import get_redis

log = get_logger(__name__)

CRM_PUSH_QUEUE_KEY: Final[str] = "crm_push_queue"
CRM_STATUS_QUEUE_KEY: Final[str] = "crm_status_queue"

_PAYLOAD_VERSION: Final[int] = 1


# --- HMAC -------------------------------------------------------------------


def build_signature(secret: str, timestamp: int, raw_body: bytes) -> str:
    """HMAC-SHA256 hex digest over ``str(ts).encode() + b"." + raw_body``.

    Byte-for-byte identical to the CRM receiver (ADR-044 §3). The ``bytes``
    concatenation avoids the f-string-over-bytes trap that would embed a
    ``repr`` instead of the raw bytes.
    """
    mac_input = str(timestamp).encode("ascii") + b"." + raw_body
    return hmac.new(secret.encode("utf-8"), mac_input, hashlib.sha256).hexdigest()


def _serialize(body: dict[str, object]) -> bytes:
    """Serialise the JSON body ONCE — these exact bytes are signed AND sent."""
    return json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


async def _post_signed(url: str, body: dict[str, object]) -> httpx.Response:
    """POST ``body`` to ``url`` with the HMAC headers over the raw bytes.

    The raw body is serialised once and passed to httpx as ``content`` (NOT
    ``json=``) so the transmitted bytes are precisely the signed bytes. TLS
    verification is on; a bounded total timeout applies.
    """
    settings = get_settings()
    raw_body = _serialize(body)
    ts = int(time.time())
    signature = build_signature(settings.CRM_PUSH_SECRET, ts, raw_body)
    headers = {
        "Content-Type": "application/json",
        "X-Mail-Signature": f"sha256={signature}",
        "X-Mail-Timestamp": str(ts),
    }
    timeout = httpx.Timeout(float(settings.CRM_PUSH_HTTP_TIMEOUT_SECONDS))
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False, verify=True) as client:
        return await client.post(url, content=raw_body, headers=headers)


def _ingest_url() -> str:
    return f"{get_settings().CRM_INGEST_URL.rstrip('/')}/api/mail/ingest"


def _status_url() -> str:
    return f"{get_settings().CRM_MAILBOX_STATUS_URL.rstrip('/')}/api/mail/mailbox-status"


def _iso(value: datetime) -> str:
    """ISO 8601 in UTC. Coerces a naive value to UTC defensively."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def _iso_or_none(value: datetime | None) -> str | None:
    return None if value is None else _iso(value)


# --- Queue payloads ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _PushQueuePayload:
    """Wire format of items in Redis ``crm_push_queue``."""

    message_id: int
    source: str  # "sync" | "recovery"

    @classmethod
    def from_json(cls, raw: str) -> _PushQueuePayload | None:
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return None
        if not isinstance(data, dict):
            return None
        mid = data.get("message_id")
        if not isinstance(mid, int):
            return None
        source = data.get("source")
        if not isinstance(source, str):
            source = "sync"
        return cls(message_id=int(mid), source=source)

    def to_json(self) -> str:
        return json.dumps(
            {"v": _PAYLOAD_VERSION, "message_id": self.message_id, "source": self.source},
            separators=(",", ":"),
        )


def parse_status_payload(raw: str) -> int | None:
    """Parse a ``crm_status_queue`` item into a ``mail_account_id``."""
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    acc_id = data.get("mail_account_id")
    return int(acc_id) if isinstance(acc_id, int) else None


# --- Enqueue helpers (Redis only, no DB session) ---------------------------


async def enqueue_push_ids(message_ids: list[int], *, source: str = "sync") -> int:
    """LPUSH ``message_id`` entries onto ``crm_push_queue``.

    Best-effort — the caller MUST wrap this in a try/except that logs but does
    not re-raise: a Redis outage must never abort ``sync_cycle``. Returns the
    number of items pushed.
    """
    if not message_ids:
        return 0
    redis = get_redis()
    items = [_PushQueuePayload(int(mid), source).to_json() for mid in message_ids]
    await cast(Awaitable[int], redis.lpush(CRM_PUSH_QUEUE_KEY, *items))
    return len(items)


async def enqueue_crm_status(account_id: int) -> None:
    """LPUSH one mailbox-status event onto ``crm_status_queue``.

    Carries only the ``mail_account_id`` — the dispatcher loads the current
    row and sends its live status snapshot, so ordering / staleness is
    irrelevant (the CRM mirrors current state and dedups the down-alert on its
    side, ADR-044 §3). Best-effort; caller wraps in try/except.
    """
    redis = get_redis()
    payload = json.dumps(
        {"v": _PAYLOAD_VERSION, "mail_account_id": int(account_id)},
        separators=(",", ":"),
    )
    await cast(Awaitable[int], redis.lpush(CRM_STATUS_QUEUE_KEY, payload))


async def enqueue_crm_status_best_effort(account_id: int) -> None:
    """The ONE mailbox-status hook helper (ADR-0046 §2) — every H-point calls this.

    Single implementation shared by all hook points (H1-H4 in
    ``worker/app/sync_cycle.py``, H5/H6 in ``backend/app/accounts``, H7a in
    ``backend/app/oauth/service.py``): gate on ``crm_status_enabled``
    (``CRM_MAILBOX_STATUS_URL`` + ``CRM_PUSH_SECRET``), enqueue, log — wrapped
    best-effort so a Redis outage NEVER breaks the sync cycle or an admin/API
    request.

    MUST be called strictly AFTER the COMMIT of the transaction that changed the
    status (ADR-0046 §2): the dispatcher loads the live snapshot from the DB, so
    an enqueue inside the open transaction can be served the pre-commit state and
    the mirrored status would stick until the next status event (for a mailbox
    deactivated via ``is_active=false`` — forever: it drops out of
    ``list_active()`` and never syncs again).
    """
    if not get_settings().crm_status_enabled:
        return
    try:
        await enqueue_crm_status(account_id)
        log.info("crm_status_enqueued", mail_account_id=account_id)
    except Exception as exc:
        log.warning(
            "crm_status_enqueue_failed",
            mail_account_id=account_id,
            detail=str(exc)[:200],
        )


# --- Payload builders -------------------------------------------------------


def _message_to_ingest(m: Message) -> dict[str, object]:
    """Map a ``messages`` row to a ``MailIngestMessage`` (ADR-044 §3).

    Attachments are NOT included. ``internal_date`` is ISO 8601 UTC.
    """
    return {
        "mail_account_id": int(m.mail_account_id),
        "uidvalidity": int(m.uidvalidity),
        "uid": int(m.uid),
        "message_id_header": m.message_id_header,
        "subject": m.subject,
        "from_addr": m.from_addr,
        "from_name": m.from_name,
        "to_addrs": m.to_addrs,
        "cc_addrs": m.cc_addrs,
        "internal_date": _iso(m.internal_date),
        "body_text": m.body_text,
        "body_html": m.body_html,
        "in_reply_to": m.in_reply_to,
        "refs_header": m.refs_header,
    }


def _account_to_status(acc: MailAccount) -> dict[str, object]:
    """Map a ``mail_accounts`` row to the status-channel body (ADR-044 §3)."""
    return {
        "mail_account_id": int(acc.id),
        "is_active": bool(acc.is_active),
        "last_synced_at": _iso_or_none(acc.last_synced_at),
        "last_sync_error": acc.last_sync_error,
        "consecutive_failures": int(acc.consecutive_failures),
    }


@dataclass(frozen=True, slots=True)
class PushResult:
    """Outcome of one ingest batch POST."""

    ok: bool
    delivered: int
    marked: int
    missing: int


# --- Services ---------------------------------------------------------------


class CrmPushService:
    """Ingest-channel push + recovery (ADR-0043 §2)."""

    def __init__(self, session: AsyncSession) -> None:
        self._db = session
        self._messages = MessagesRepo(session)

    async def push_message_ids(self, message_ids: list[int]) -> PushResult:
        """POST a batch of messages to ``/api/mail/ingest``.

        On ``2xx`` the delivered ids are stamped ``pushed_at=now()`` (guarded,
        idempotent). On any non-2xx / transport error the batch is left
        unmarked and ``ok=False`` is returned so the caller can re-enqueue for
        retry (the CRM ingest is idempotent, so a re-push never duplicates).
        Ids that no longer exist (retention) are silently dropped.
        """
        messages = await self._messages.list_for_crm_push(message_ids)
        if not messages:
            return PushResult(ok=True, delivered=0, marked=0, missing=len(message_ids))
        body: dict[str, object] = {"messages": [_message_to_ingest(m) for m in messages]}
        try:
            resp = await _post_signed(_ingest_url(), body)
        except httpx.HTTPError as exc:
            log.warning(
                "crm_push_transport_error",
                detail=str(exc)[:200],
                error_type=type(exc).__name__,
                batch=len(messages),
            )
            return PushResult(ok=False, delivered=0, marked=0, missing=0)
        if 200 <= resp.status_code < 300:
            marked = await self._messages.mark_pushed([int(m.id) for m in messages])
            log.info(
                "crm_push_delivered",
                batch=len(messages),
                marked=marked,
                status=resp.status_code,
            )
            return PushResult(ok=True, delivered=len(messages), marked=marked, missing=0)
        log.warning(
            "crm_push_rejected",
            status=resp.status_code,
            batch=len(messages),
            body_excerpt=resp.text[:200],
        )
        return PushResult(ok=False, delivered=0, marked=0, missing=0)

    async def list_recovery_candidates(self, *, window_hours: int, limit: int) -> list[int]:
        """``messages.id`` still ``pushed_at IS NULL`` within the window."""
        window_start = datetime.now(UTC) - timedelta(hours=window_hours)
        return await self._messages.list_pending_push(window_start=window_start, limit=limit)


class CrmStatusService:
    """Mailbox status-channel push (ADR-0043 §2 / ADR-044 §3)."""

    def __init__(self, session: AsyncSession) -> None:
        self._db = session
        self._accounts = MailAccountsRepo(session)

    async def push_status(self, account_id: int) -> bool:
        """POST the live status snapshot of ``account_id`` to the CRM.

        Returns ``True`` on ``2xx`` (or when the account no longer exists —
        nothing to deliver), ``False`` on non-2xx / transport error so the
        caller can re-enqueue. Idempotency of the down-alert is on the CRM
        side (``down_alert_sent_at``), so re-sending the same status is safe.
        """
        acc = await self._accounts.get_by_id(account_id)
        if acc is None:
            log.info("crm_status_account_missing", mail_account_id=account_id)
            return True
        body = _account_to_status(acc)
        try:
            resp = await _post_signed(_status_url(), body)
        except httpx.HTTPError as exc:
            log.warning(
                "crm_status_transport_error",
                mail_account_id=account_id,
                detail=str(exc)[:200],
                error_type=type(exc).__name__,
            )
            return False
        if 200 <= resp.status_code < 300:
            log.info(
                "crm_status_delivered",
                mail_account_id=account_id,
                is_active=bool(acc.is_active),
                status=resp.status_code,
            )
            return True
        log.warning(
            "crm_status_rejected",
            mail_account_id=account_id,
            status=resp.status_code,
            body_excerpt=resp.text[:200],
        )
        return False
