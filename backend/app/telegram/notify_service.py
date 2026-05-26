"""Telegram push-notification enqueue + dispatch helpers (ADR-0022 §2).

This module owns:

- :meth:`TelegramNotifyService.enqueue_message` — resolve recipients for
  one newly-saved message and LPUSH each pending notification into the
  Redis ``tg_notify_queue``. Called from the worker's ``sync_cycle`` after
  the per-account transaction commits (so messages and message_tags are
  visible to the SQL recipient query).
- :meth:`TelegramNotifyService.dispatch_one` — pop a queue item, send the
  Bot API notification, finalise / rollback / mark-dead. Used by the
  worker's APScheduler dispatcher job.

The service does **not** open its own DB transactions; the caller wraps
the work in ``async with db.begin():`` to keep audit + ``telegram_links``
mutations atomic.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Final, cast

from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.rate_limit import LIMIT_TG_SEND_PER_CHAT, Limit, try_consume
from backend.app.repositories.mail_accounts import MailAccountsRepo
from backend.app.repositories.telegram_notifications import (
    NotifyRecipient,
    TelegramNotificationsRepo,
)
from backend.app.telegram.bot import (
    SendNotificationResult,
    send_notification,
)
from backend.app.telegram.notify_format import (
    format_notification,
    html_to_plain,
    normalize_preview,
)
from backend.app.telegram.sso_service import (
    TG_NOTIFY_QUEUE_KEY,
    TelegramSSOService,
)
from shared.config import get_settings
from shared.logging import get_logger
from shared.redis_client import get_redis

log = get_logger(__name__)

# Sentinel — opaque marker that the recovery scan injects to distinguish
# "freshly produced from sync_cycle" from "recovery_scan retry". Kept short
# to avoid bloating the Redis queue.
_PAYLOAD_VERSION: Final[int] = 1


@dataclass(frozen=True, slots=True)
class _QueuePayload:
    """Wire format of items in Redis ``tg_notify_queue``."""

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


class TelegramNotifyService:
    """Enqueue + dispatch helpers.

    Construct with an :class:`AsyncSession`; for the dispatch path the
    session must already be inside a transaction (the SSO sub-service
    writes audit log rows).
    """

    def __init__(self, session: AsyncSession) -> None:
        self._db = session
        self._notifications = TelegramNotificationsRepo(session)
        self._accounts = MailAccountsRepo(session)
        self._sso = TelegramSSOService(session)

    # --- Enqueue ----------------------------------------------------------

    async def enqueue_message_ids(self, message_ids: list[int]) -> int:
        """LPUSH ``message_id`` entries into the Redis queue.

        ``message_ids`` should contain only messages that actually have
        any ``message_tags`` row — the recipient SQL filters anyway, but
        the per-message Redis cost is non-trivial and we want the queue
        to stay narrow.

        Returns the number of items pushed. The caller (worker) should
        always wrap this call in a try/except that logs but does not
        re-raise: a failure to LPUSH must never abort ``sync_cycle``.
        """
        if not message_ids:
            return 0
        redis = get_redis()
        items = [_QueuePayload(message_id=int(mid), source="sync").to_json() for mid in message_ids]
        # LPUSH variadic — single round-trip.
        # redis-py async client annotates lpush as returning Awaitable[int] |
        # int (the union accommodates the sync facade). awaiting on the
        # union is correct at runtime; mypy can't narrow it for us.
        # ``lpush`` is typed ``Awaitable[int] | int`` (redis-py sync/async
        # union); the runtime here is always async. ``cast`` keeps both
        # local mypy and CI mypy quiet without an ``# type: ignore`` that
        # CI would flag as unused.
        await cast(Awaitable[int], redis.lpush(TG_NOTIFY_QUEUE_KEY, *items))
        return len(items)

    async def enqueue_recovery(self, message_ids: list[int]) -> int:
        """Same as :meth:`enqueue_message_ids` but tagged ``source=recovery``
        for traceability in dispatcher logs."""
        if not message_ids:
            return 0
        redis = get_redis()
        items = [
            _QueuePayload(message_id=int(mid), source="recovery").to_json() for mid in message_ids
        ]
        # redis-py async client annotates lpush as returning Awaitable[int] |
        # int (the union accommodates the sync facade). awaiting on the
        # union is correct at runtime; mypy can't narrow it for us.
        # ``lpush`` is typed ``Awaitable[int] | int`` (redis-py sync/async
        # union); the runtime here is always async. ``cast`` keeps both
        # local mypy and CI mypy quiet without an ``# type: ignore`` that
        # CI would flag as unused.
        await cast(Awaitable[int], redis.lpush(TG_NOTIFY_QUEUE_KEY, *items))
        return len(items)

    # --- Dispatch ---------------------------------------------------------

    async def dispatch_one_payload(self, payload_raw: str) -> None:
        """Process a single queue payload.

        Contract:

        1. Parse the payload; malformed → log + skip (no retry — the item
           cannot become well-formed by retrying).
        2. Load the message and account meta in one DB hit each.
        3. Resolve the recipient set via the SQL in
           :class:`TelegramNotificationsRepo`.
        4. Resolve the tag list **once** per message (round-12 bug A:
           used to be per-recipient; group members had no tags of their
           own and were silently dropped). Round-31: the tag list MAY be
           empty (notify-about-all messages) — there is **no** early return
           on empty tags; the notification still ships without a tag line.
        5. For each recipient:
           a. Round-31 per-chat throttle (§2.9): non-blocking
              ``try_consume(LIMIT_TG_SEND_PER_CHAT, chat_id)`` **before**
              ``try_reserve``. If the per-chat budget is exhausted →
              ``continue`` (do NOT reserve a row, do NOT re-enqueue). The
              recovery scan (hourly, §2.8) picks the message up later — this
              avoids the busy-loop a hot re-enqueue would cause under a
              sustained ``inflow > cap``.
           b. ``try_reserve`` — if the row already existed, skip silently.
           c. Format the text using the message-level tag list.
           d. Call :func:`send_notification`.
           e. Handle the outcome:
              - ``ok`` → ``mark_sent`` with ``telegram_message_id``.
              - ``dead`` → mark the link dead + keep the row (no retry).
              - ``retry_after`` → leave the row claimed; re-enqueue the
                whole message_id (next tick will pick it up; idempotency
                guarantees we won't double-deliver other recipients).
              - ``transient`` → ``rollback`` the row and re-enqueue.
              - ``disabled`` → leave the claimed row; nothing else to do.
        """
        payload = _QueuePayload.from_json(payload_raw)
        if payload is None:
            log.warning(
                "tg_notify_dispatch_malformed",
                raw_excerpt=payload_raw[:200],
            )
            return

        # Unscoped get — dispatch is a system action and must see every
        # message regardless of visibility rules. The recipient SQL applies
        # ADR-0019 visibility per-user later.
        from shared.models import Message  # local import to avoid cycle

        message = await self._db.get(Message, payload.message_id)
        if message is None:
            # Message gone (e.g. retention cleanup ran after enqueue).
            log.info(
                "tg_notify_dispatch_message_missing",
                message_id=payload.message_id,
                source=payload.source,
            )
            return

        account = await self._accounts.get_by_id(message.mail_account_id)
        if account is None:
            # Account gone too — same outcome as above.
            log.info(
                "tg_notify_dispatch_account_missing",
                message_id=payload.message_id,
                mail_account_id=message.mail_account_id,
            )
            return

        recipients = await self._notifications.list_recipients_for_message(
            message_id=payload.message_id
        )
        if not recipients:
            return

        # Round-12 bug A: tags are now resolved **once per message** (not
        # per recipient). Every group member receives the same tag-name
        # list, which matches the visibility model (if you can see the
        # mailbox, you see the tags applied to its messages). Auto-tagging
        # is owner-scoped, so without this change a leader's group-mates
        # got no notification at all (recipient SQL ANDed on per-user tag).
        message_tags = await self._notifications.list_tags_for_message(
            message_id=payload.message_id
        )
        # Round-31 (ADR-0022 §2.5): NO early return on empty tags. With
        # TG_NOTIFY_ALL_MESSAGES on (default) a message may legitimately have
        # no tags — we still notify, just without the (optional) tag line.
        # Round-21 (bug #2): collapse sibling tags by (name, color) — the
        # auto-tagging worker creates one ``tags`` row per team-member,
        # but the notification text should show each logical tag once.
        seen_tag_keys: set[tuple[str, str]] = set()
        tag_names: list[str] = []
        for t in message_tags:
            key = (t.name, t.color)
            if key in seen_tag_keys:
                continue
            seen_tag_keys.add(key)
            tag_names.append(t.name)

        acc_label = account.display_name or account.email
        from_label = message.from_name or message.from_addr

        # Round-34 (ADR-0022 §2.5): compute the body preview ONCE per message
        # (not per recipient) — all recipients see the same teaser. Source is
        # the plain-text part; fall back to the HTML part stripped to plain.
        # NB: use ``.strip()`` not truthiness — ``body_text`` is NOT NULL with
        # a ``''`` server default, and a whitespace-only value must not block
        # the HTML fallback. The slice happens in Python, never in SQL.
        if message.body_text.strip():
            raw_preview = message.body_text
        else:
            raw_preview = html_to_plain(message.body_html)
        body_preview = normalize_preview(raw_preview)
        # ``subject`` is passed as-is; escape + SUBJECT_MAX truncation happen
        # inside ``format_notification``.
        subject = message.subject

        # Track whether any recipient asked for a retry — if so, we need
        # to put the message back on the queue so other-or-same recipients
        # can be tried again after the back-off.
        needs_retry = False
        retry_sleep_seconds = 0

        # Round-31 (ADR-0022 §2.9): per-chat throttle capacity is read once
        # from lru-cached settings and applied per-recipient at consume-time
        # (same override pattern as LIMIT_WEBHOOK_TEST) — no redeploy needed
        # to retune ``TG_SEND_PER_CHAT_PER_MINUTE``.
        throttle_limit = Limit(
            name=LIMIT_TG_SEND_PER_CHAT.name,
            capacity=get_settings().TG_SEND_PER_CHAT_PER_MINUTE,
            window_seconds=LIMIT_TG_SEND_PER_CHAT.window_seconds,
        )

        for recipient in recipients:
            # Per-chat throttle BEFORE try_reserve (§2.9): if the per-chat
            # budget is exhausted, skip this recipient now WITHOUT reserving a
            # telegram_notifications row and WITHOUT a hot re-enqueue. Leaving
            # the (message_id, user_id) row absent lets the hourly recovery
            # scan (§2.8, per-recipient NOT EXISTS) pick this recipient up
            # later — natural ~1h backoff, no busy-loop under sustained flood.
            if not await try_consume(throttle_limit, key=str(recipient.telegram_user_id)):
                log.info(
                    "tg_notify_throttled",
                    message_id=payload.message_id,
                    user_id=recipient.user_id,
                    source=payload.source,
                )
                continue
            outcome = await self._dispatch_one_recipient(
                payload=payload,
                recipient=recipient,
                acc_label=acc_label,
                from_label=from_label,
                tag_names=tag_names,
                subject=subject,
                body_preview=body_preview,
            )
            if outcome is None:
                continue
            if outcome.kind == "retry_after":
                needs_retry = True
                retry_sleep_seconds = max(retry_sleep_seconds, outcome.retry_after_sec or 1)
            elif outcome.kind == "transient":
                needs_retry = True

        if needs_retry:
            # Re-enqueue: dispatch_one will eventually re-process; the
            # UNIQUE on (message_id, user_id) ensures already-delivered
            # recipients are skipped.
            await self.enqueue_recovery([payload.message_id])
            log.info(
                "tg_notify_dispatch_requeued",
                message_id=payload.message_id,
                source=payload.source,
                retry_after_sec=retry_sleep_seconds,
            )

    async def _dispatch_one_recipient(
        self,
        *,
        payload: _QueuePayload,
        recipient: NotifyRecipient,
        acc_label: str,
        from_label: str,
        tag_names: list[str],
        subject: str | None,
        body_preview: str,
    ) -> SendNotificationResult | None:
        """Process one ``(message_id, recipient)`` pair. Returns the
        :class:`SendNotificationResult` if a Bot API call was attempted,
        else ``None`` (e.g. row already existed).

        Round-12 bug A: ``tag_names`` is now resolved once by the caller
        and shared across all recipients — see :meth:`dispatch_one_payload`.
        Round-34: ``subject`` / ``body_preview`` are likewise computed once
        per message by the caller and shared across recipients.
        """
        notification_id = await self._notifications.try_reserve(
            message_id=payload.message_id, user_id=recipient.user_id
        )
        if notification_id is None:
            # Already delivered (or claimed) — skip.
            return None

        text_html = format_notification(
            acc_label=acc_label,
            from_label=from_label,
            tag_names=tag_names,
            subject=subject,
            body_preview=body_preview,
        )

        outcome = await send_notification(
            chat_id=recipient.telegram_user_id,
            text_html=text_html,
            message_id=payload.message_id,
        )

        if outcome.kind == "ok":
            await self._notifications.mark_sent(
                notification_id=notification_id,
                telegram_message_id=outcome.telegram_message_id,
            )
            log.info(
                "tg_notify_sent",
                message_id=payload.message_id,
                user_id=recipient.user_id,
                telegram_message_id=outcome.telegram_message_id,
                source=payload.source,
            )
            return outcome

        if outcome.kind == "dead":
            # Keep the row (with sent_at NULL) as an audit marker; mark
            # the link dead so subsequent dispatches skip it at the SQL
            # recipient stage.
            await self._sso.mark_link_dead(
                telegram_user_id=recipient.telegram_user_id,
                user_id=recipient.user_id,
                reason=outcome.detail or "bot_blocked_or_chat_gone",
            )
            log.info(
                "tg_notify_dead",
                message_id=payload.message_id,
                user_id=recipient.user_id,
                detail=(outcome.detail or "")[:200],
            )
            return outcome

        if outcome.kind == "disabled":
            # Bot is not configured. Keep the claim row to avoid endless
            # re-attempts; ops can re-enable and run recovery_scan.
            log.info(
                "tg_notify_skipped_bot_disabled",
                message_id=payload.message_id,
                user_id=recipient.user_id,
            )
            return outcome

        # retry_after / transient — release the claim so a re-enqueue can
        # try this recipient again.
        await self._notifications.rollback(notification_id=notification_id)
        log.info(
            "tg_notify_will_retry",
            message_id=payload.message_id,
            user_id=recipient.user_id,
            kind=outcome.kind,
            retry_after_sec=outcome.retry_after_sec,
            detail=(outcome.detail or "")[:200],
        )
        return outcome
