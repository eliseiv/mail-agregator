"""Outbound webhook dispatcher (ADR-0023 §3).

Two responsibilities:

- :meth:`WebhookDispatchService.enqueue_message_ids` — LPUSH new
  ``message_id`` values into the Redis ``webhook_dispatch_queue`` after
  ``sync_cycle`` commits. Symmetric to
  :meth:`TelegramNotifyService.enqueue_message_ids`.
- :meth:`WebhookDispatchService.dispatch_one_payload` — process one
  queue item: resolve the recipient webhook, claim a delivery row,
  POST, handle 2xx/4xx/5xx/410/network outcomes per ADR-0023 §3.4.

This service does not open transactions itself; the worker job wraps
each call in ``async with make_session() as s, s.begin():`` so the
audit write (mark_dead etc.) is atomic with the delivery row.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final, cast

if TYPE_CHECKING:
    from shared.models import MailAccount, Message

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.audit import AuditWriter
from backend.app.repositories.mail_accounts import MailAccountsRepo
from backend.app.repositories.webhooks import (
    WebhookDeliveriesRepo,
    WebhookRecipient,
    WebhooksRepo,
    WebhookTeamTag,
)
from shared.config import get_settings
from shared.crypto import InvalidTag, decrypt_webhook_secret
from shared.logging import get_logger
from shared.redis_client import get_redis
from shared.url_safety import WebhookUrlError, validate_outbound_url

log = get_logger(__name__)

WEBHOOK_DISPATCH_QUEUE_KEY: Final[str] = "webhook_dispatch_queue"
_PAYLOAD_VERSION: Final[int] = 1

# Receiver responses are clamped to 16 KiB body for both the payload
# (``body_text``) and 500 B for the ``response_excerpt`` stored in DB.
_BODY_TEXT_MAX_CHARS: Final[int] = 16384


# --- Queue wire format ------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _QueuePayload:
    """Wire format of items in ``webhook_dispatch_queue`` (matches the TG
    payload to keep operational reasoning between the two queues
    symmetrical)."""

    message_id: int
    source: str  # "sync" | "recovery"

    @classmethod
    def from_json(cls, raw: str) -> _QueuePayload | None:
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return None
        if not isinstance(data, dict):
            return None
        mid_raw = data.get("message_id")
        if not isinstance(mid_raw, int):
            return None
        source = data.get("source")
        if not isinstance(source, str):
            source = "sync"
        return cls(message_id=int(mid_raw), source=source)

    def to_json(self) -> str:
        return json.dumps(
            {
                "v": _PAYLOAD_VERSION,
                "message_id": self.message_id,
                "source": self.source,
            },
            separators=(",", ":"),
        )


# --- Service ----------------------------------------------------------------


class WebhookDispatchService:
    def __init__(self, session: AsyncSession) -> None:
        self._db = session
        self._webhooks = WebhooksRepo(session)
        self._deliveries = WebhookDeliveriesRepo(session)
        self._accounts = MailAccountsRepo(session)
        self._audit = AuditWriter(session)

    # --- Enqueue ----------------------------------------------------------

    async def enqueue_message_ids(self, message_ids: list[int]) -> int:
        """LPUSH ``message_id`` entries from ``sync_cycle``.

        Returns the number of items pushed. Callers (worker
        ``sync_cycle``) wrap this in try/except — a failure to LPUSH must
        never abort the sync cycle.
        """
        if not message_ids:
            return 0
        redis = get_redis()
        items = [_QueuePayload(message_id=int(mid), source="sync").to_json() for mid in message_ids]
        # See identical comment in :mod:`backend.app.telegram.notify_service`
        # for the ``cast`` rationale — redis-py types ``lpush`` as
        # ``Awaitable[int] | int``; runtime is always async here.
        await cast(Awaitable[int], redis.lpush(WEBHOOK_DISPATCH_QUEUE_KEY, *items))
        return len(items)

    async def enqueue_recovery(self, message_ids: list[int]) -> int:
        """Same as :meth:`enqueue_message_ids` but tags ``source=recovery``."""
        if not message_ids:
            return 0
        redis = get_redis()
        items = [
            _QueuePayload(message_id=int(mid), source="recovery").to_json() for mid in message_ids
        ]
        await cast(Awaitable[int], redis.lpush(WEBHOOK_DISPATCH_QUEUE_KEY, *items))
        return len(items)

    # --- Dispatch ---------------------------------------------------------

    async def dispatch_one_payload(self, payload_raw: str) -> None:
        """Process a single queue payload.

        Algorithm (ADR-0023 §3.4):

        1. Parse the payload — malformed → log + skip.
        2. Load message + account + group; resolve the recipient webhook.
        3. Claim a delivery row (UNIQUE handles re-runs).
        4. Build the payload, decrypt the secret, POST.
        5. Branch on response: 2xx / 4xx / 410 / 5xx-or-timeout-or-network.

        Branching logic is split into :meth:`_handle_response` and
        related helpers to keep this orchestrator readable and to avoid
        ``ruff`` PLR0911 (too many return statements).
        """
        payload = _QueuePayload.from_json(payload_raw)
        if payload is None:
            log.warning(
                "webhook_dispatch_malformed",
                raw_excerpt=payload_raw[:200],
            )
            return

        ctx = await self._load_context(payload)
        if ctx is None:
            return

        message, account, recipient = ctx

        # Idempotent claim.
        delivery_id = await self._deliveries.try_reserve(
            webhook_id=recipient.webhook_id, message_id=payload.message_id
        )
        if delivery_id is None:
            # Already delivered (or another claim in-flight) — skip.
            return

        # Aggregate tags for the team — defensive check + payload input.
        team_tags = await self._deliveries.list_tags_for_team(
            message_id=payload.message_id, group_id=recipient.group_id
        )
        if not team_tags:
            await self._deliveries.rollback(delivery_id=delivery_id)
            log.info(
                "webhook_dispatch_no_tags",
                webhook_id=recipient.webhook_id,
                message_id=payload.message_id,
            )
            return

        # Decrypt + URL re-validate; either failure is terminal for this
        # webhook (instant dead).
        prepared = await self._prepare_for_post(recipient=recipient, delivery_id=delivery_id)
        if prepared is None:
            return
        secret_plaintext, url = prepared

        payload_body = self._build_payload_body(
            message=message,
            account=account,
            recipient=recipient,
            delivery_id=delivery_id,
            team_tags=team_tags,
        )
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "X-Webhook-Secret": secret_plaintext,
            "User-Agent": "mas-webhook/1.0",
            "X-Webhook-Event": "message_tagged",
            "X-Webhook-Delivery-Id": str(delivery_id),
        }

        response = await self._post_with_handling(
            url=url,
            payload_body=payload_body,
            headers=headers,
            recipient=recipient,
            delivery_id=delivery_id,
            message_id=payload.message_id,
        )
        if response is None:
            return  # transport-level failure already handled inside.

        await self._handle_response(
            response_status_code=response.status_code,
            response_text=response.text,
            recipient=recipient,
            delivery_id=delivery_id,
            message_id=payload.message_id,
            source=payload.source,
        )

    # --- Dispatch sub-steps ----------------------------------------------

    async def _load_context(
        self, payload: _QueuePayload
    ) -> tuple[Message, MailAccount, WebhookRecipient] | None:
        """Load (message, account, recipient) for ``payload``.

        Returns ``None`` if any of the inputs is missing (message
        deleted, account gone, no active webhook). The recipient lookup
        applies the §3.2 SQL (is_active / dead_at / history filter / tag
        existence).
        """
        from shared.models import Message  # local import to avoid cycle

        message = await self._db.get(Message, payload.message_id)
        if message is None:
            log.info(
                "webhook_dispatch_message_missing",
                message_id=payload.message_id,
                source=payload.source,
            )
            return None

        account = await self._accounts.get_by_id(message.mail_account_id)
        if account is None:
            log.info(
                "webhook_dispatch_account_missing",
                message_id=payload.message_id,
                mail_account_id=message.mail_account_id,
            )
            return None

        recipient = await self._webhooks.find_active_for_message(
            message_id=payload.message_id, mail_account_id=account.id
        )
        if recipient is None:
            return None

        return message, account, recipient

    async def _prepare_for_post(
        self, *, recipient: WebhookRecipient, delivery_id: int
    ) -> tuple[str, str] | None:
        """Decrypt the secret and re-validate the URL.

        Returns ``(secret_plaintext, url)`` on success, ``None`` after
        marking the webhook dead on either failure. The caller has
        already claimed the delivery row, so we roll it back on failure
        (the row would never have ``sent_at`` set otherwise, but rolling
        back keeps the table free of orphan rows).
        """
        try:
            secret_plaintext = decrypt_webhook_secret(
                recipient.secret_encrypted, recipient.webhook_id
            )
        except InvalidTag:
            await self._deliveries.rollback(delivery_id=delivery_id)
            await self._webhooks.mark_dead(recipient.webhook_id, reason="secret_decrypt_failed")
            await self._audit.log(
                actor_user_id=0,
                action="webhook_dead_marked",
                details={
                    "webhook_id": recipient.webhook_id,
                    "reason": "secret_decrypt_failed",
                },
            )
            log.warning(
                "webhook_secret_decrypt_failed",
                webhook_id=recipient.webhook_id,
                group_id=recipient.group_id,
            )
            return None

        try:
            url = validate_outbound_url(recipient.url)
        except WebhookUrlError as exc:
            await self._deliveries.rollback(delivery_id=delivery_id)
            await self._webhooks.mark_dead(recipient.webhook_id, reason=exc.reason)
            await self._audit.log(
                actor_user_id=0,
                action="webhook_dead_marked",
                details={
                    "webhook_id": recipient.webhook_id,
                    "reason": exc.reason,
                },
            )
            log.warning(
                "webhook_url_unsafe_at_dispatch",
                webhook_id=recipient.webhook_id,
                reason=exc.reason,
            )
            return None

        return secret_plaintext, url

    def _build_payload_body(
        self,
        *,
        message: Message,
        account: MailAccount,
        recipient: WebhookRecipient,
        delivery_id: int,
        team_tags: list[WebhookTeamTag],
    ) -> dict[str, object]:
        """Construct the ``event=message_tagged`` payload body (ADR-0023 §2.9)."""
        body_text_raw = message.body_text or ""
        body_text = body_text_raw[:_BODY_TEXT_MAX_CHARS]
        body_truncated = len(body_text_raw) > _BODY_TEXT_MAX_CHARS
        return {
            "event": "message_tagged",
            "timestamp": _utc_now_iso(),
            "webhook_id": recipient.webhook_id,
            "delivery_id": delivery_id,
            "team": {"id": recipient.group_id},
            "message": {
                "id": message.id,
                "internal_date": _iso_or_none(message.internal_date),
                "from_addr": message.from_addr,
                "from_name": message.from_name,
                "subject": message.subject,
                "body_text": body_text,
                "body_truncated": body_truncated,
                "mail_account": {
                    "id": account.id,
                    "email": account.email,
                    "display_name": account.display_name,
                },
                "tags": [
                    {
                        "id": t.id,
                        "name": t.name,
                        "color": t.color,
                    }
                    for t in team_tags
                ],
            },
        }

    async def _post_with_handling(
        self,
        *,
        url: str,
        payload_body: dict[str, object],
        headers: dict[str, str],
        recipient: WebhookRecipient,
        delivery_id: int,
        message_id: int,
    ) -> httpx.Response | None:
        """POST and handle transport-level failures.

        Returns the response on success (any HTTP status). Returns
        ``None`` on transport-level error (timeout, network, request
        error) after rolling back the delivery row + touching
        ``last_error`` (no failure-counter increment — transient).
        """
        settings = get_settings()
        timeout = httpx.Timeout(float(settings.WEBHOOK_HTTP_TIMEOUT_SECONDS))
        try:
            async with httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=False,
                verify=True,
            ) as client:
                return await client.post(url, json=payload_body, headers=headers)
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            await self._deliveries.rollback(delivery_id=delivery_id)
            await self._webhooks.touch_last_error(
                recipient.webhook_id, f"network: {type(exc).__name__}"
            )
            log.warning(
                "webhook_dispatch_transient",
                webhook_id=recipient.webhook_id,
                message_id=message_id,
                error_type=type(exc).__name__,
            )
            return None
        except httpx.RequestError as exc:
            await self._deliveries.rollback(delivery_id=delivery_id)
            await self._webhooks.touch_last_error(
                recipient.webhook_id, f"request_error: {type(exc).__name__}"
            )
            log.warning(
                "webhook_dispatch_request_error",
                webhook_id=recipient.webhook_id,
                error_type=type(exc).__name__,
            )
            return None

    async def _handle_response(
        self,
        *,
        response_status_code: int,
        response_text: str,
        recipient: WebhookRecipient,
        delivery_id: int,
        message_id: int,
        source: str,
    ) -> None:
        """Branch on the receiver's HTTP status (ADR-0023 §3.4 steps 9-12)."""
        settings = get_settings()
        excerpt = (response_text or "")[:500]
        status_code = response_status_code

        if status_code == 410:
            await self._deliveries.mark_failed(
                delivery_id=delivery_id,
                response_code=status_code,
                response_excerpt=excerpt,
            )
            await self._webhooks.mark_dead(recipient.webhook_id, reason="410_gone")
            await self._audit.log(
                actor_user_id=0,
                action="webhook_dead_marked",
                details={"webhook_id": recipient.webhook_id, "reason": "410_gone"},
            )
            log.info(
                "webhook_dispatch_410",
                webhook_id=recipient.webhook_id,
                message_id=message_id,
            )
            return

        if 400 <= status_code < 500 and status_code not in (408, 429):
            await self._deliveries.mark_failed(
                delivery_id=delivery_id,
                response_code=status_code,
                response_excerpt=excerpt,
            )
            new_count = await self._webhooks.bump_failure(
                recipient.webhook_id,
                f"HTTP {status_code}: {excerpt[:200]}",
            )
            log.info(
                "webhook_dispatch_4xx",
                webhook_id=recipient.webhook_id,
                message_id=message_id,
                response_code=status_code,
                consecutive_failures=new_count,
            )
            if new_count >= settings.WEBHOOK_MAX_FAILURES_BEFORE_DEAD:
                await self._webhooks.mark_dead(recipient.webhook_id, reason="consecutive_4xx")
                await self._audit.log(
                    actor_user_id=0,
                    action="webhook_dead_marked",
                    details={
                        "webhook_id": recipient.webhook_id,
                        "reason": "consecutive_4xx",
                        "last_status": status_code,
                    },
                )
            return

        if status_code in (408, 429) or 500 <= status_code < 600:
            await self._deliveries.rollback(delivery_id=delivery_id)
            await self._webhooks.touch_last_error(
                recipient.webhook_id, f"HTTP {status_code} (will retry)"
            )
            log.info(
                "webhook_dispatch_retriable",
                webhook_id=recipient.webhook_id,
                message_id=message_id,
                response_code=status_code,
            )
            return

        if 200 <= status_code < 300:
            await self._deliveries.mark_sent(
                delivery_id=delivery_id,
                response_code=status_code,
                response_excerpt=excerpt,
            )
            await self._webhooks.mark_success(recipient.webhook_id)
            log.info(
                "webhook_dispatch_sent",
                webhook_id=recipient.webhook_id,
                group_id=recipient.group_id,
                message_id=message_id,
                response_code=status_code,
                source=source,
            )
            return

        # 3xx (redirect blocked) or any unexpected status — treat as
        # non-retriable client error to surface the misconfiguration.
        await self._deliveries.mark_failed(
            delivery_id=delivery_id,
            response_code=status_code,
            response_excerpt=excerpt,
        )
        await self._webhooks.touch_last_error(
            recipient.webhook_id, f"HTTP {status_code} (unexpected)"
        )
        log.info(
            "webhook_dispatch_unexpected_status",
            webhook_id=recipient.webhook_id,
            message_id=message_id,
            response_code=status_code,
        )


# --- Top-level helpers ------------------------------------------------------


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _iso_or_none(value: datetime | None) -> str | None:
    if value is None:
        return None
    # IMAP ``internal_date`` is stored TZ-aware (UTC) in our schema; ensure
    # the ``Z`` suffix for consistency with the ``timestamp`` field.
    return value.isoformat().replace("+00:00", "Z")
