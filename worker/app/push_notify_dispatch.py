"""APScheduler job: drain the push-only per-team bot queue (ADR-0027 §3).

Tick cadence: ``settings.PUSH_NOTIFY_DISPATCH_INTERVAL_SECONDS`` (default 5s).
Max-instances 1 + coalesce: see ``worker/app/main.py``.

Per tick (fire-and-forget — ADR-0027 §5):

1. ``LPOP push_notify_queue count=PUSH_NOTIFY_BATCH_SIZE`` — drain a batch.
2. For each item: load the ``Message`` + ``MailAccount`` (unscoped get), pick
   the push bot whose ``group_id`` matches ``account.group_id`` and send the
   same notification text (round-36 ``format_notification``, no team label) to
   every admin in ``settings.admin_telegram_ids``.
3. Outcomes are **only logged** — no DB tracking, no idempotency, no recovery,
   no re-enqueue, no mark-dead (ADR-0027 §5, TD-041). A loss is rare and the
   email itself is always persisted + delivered via the main bot.

This is a separate queue + a single ``LPOP`` (not embedded in
``tg_notify_dispatch``) so each ``message_id`` is processed by the push
channel exactly once regardless of the main bot's retry/re-enqueue path —
push bots have no idempotency table to dedup a double-delivery (ADR-0027 §3).
"""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Any, cast

from shared.config import get_settings
from shared.db import make_session
from shared.logging import get_logger
from shared.redis_client import get_redis

log = get_logger(__name__)

_QUEUE_KEY = "push_notify_queue"


async def push_notify_dispatch() -> None:
    """One push-dispatcher tick (ADR-0027 §3.2)."""
    settings = get_settings()
    if not settings.push_team_bots_enabled:
        # Feature off (no configured bots or no admin recipients). Nothing to
        # drain — the queue is never written to when disabled (sync_cycle §3.1).
        return

    redis = get_redis()
    batch_size = settings.PUSH_NOTIFY_BATCH_SIZE

    # ``LPOP key count=N`` returns ``[]`` (not None) for an empty list when
    # count is supplied. redis-py types the awaited result as a sync/async
    # union; the runtime here is always async — ``cast`` picks the async
    # branch in a way that satisfies both local mypy and CI mypy.
    raw_items = await cast(
        Awaitable[bytes | str | list[Any] | None],
        redis.lpop(_QUEUE_KEY, count=batch_size),
    )
    if not raw_items:
        return

    if isinstance(raw_items, bytes | str):
        # Defensive: some redis clients return a scalar for count=1.
        items: list[str] = [raw_items.decode() if isinstance(raw_items, bytes) else raw_items]
    else:
        items = [(it.decode() if isinstance(it, bytes) else it) for it in raw_items]

    log.info("push_notify_dispatch_start", batch=len(items))

    for raw in items:
        try:
            await _dispatch_one(raw)
        except Exception as exc:
            # Never propagate — keep draining the rest of the batch this tick.
            log.warning(
                "push_notify_dispatch_item_error",
                detail=str(exc)[:200],
                error_type=type(exc).__name__,
            )


async def _dispatch_one(raw: str) -> None:
    """Process one ``push_notify_queue`` payload (fire-and-forget).

    Loads the message + account, selects the push bot by ``group_id``, builds
    the round-36 notification text and sends it to every admin. Skips (with a
    debug log) when the message/account is gone, the account has no group, or
    no bot is configured for that group (ADR-0027 §9).
    """
    settings = get_settings()

    # Local imports to avoid pulling the heavy backend.app graph at module load
    # (mirrors ``tg_notify_dispatch``) and to keep redis-only ticks cheap.
    from backend.app.repositories.mail_accounts import MailAccountsRepo
    from backend.app.repositories.telegram_notifications import TelegramNotificationsRepo
    from backend.app.telegram.bot import send_notification
    from backend.app.telegram.notify_format import (
        format_notification,
        html_to_plain,
        normalize_preview,
    )
    from backend.app.telegram.notify_service import _QueuePayload
    from shared.models import Message

    payload = _QueuePayload.from_json(raw)
    if payload is None:
        log.warning("push_notify_dispatch_malformed", raw_excerpt=raw[:200])
        return

    async with make_session() as s:
        # Unscoped get — dispatch is a system action (ADR-0027 §3.2),
        # mirroring notify_service.dispatch_one_payload.
        message = await s.get(Message, payload.message_id)
        if message is None:
            log.debug("push_team_message_missing", message_id=payload.message_id)
            return

        account = await MailAccountsRepo(s).get_by_id(message.mail_account_id)
        if account is None:
            log.debug(
                "push_team_account_missing",
                message_id=payload.message_id,
                mail_account_id=message.mail_account_id,
            )
            return

        group_id = account.group_id
        if group_id is None:
            # Personal mailbox / message outside any team — by design not
            # covered by the push channel (ADR-0027 §9).
            log.debug("push_team_skip_no_group", message_id=payload.message_id)
            return

        bot = next((b for b in settings.push_team_bots if b.group_id == group_id), None)
        if bot is None:
            # Team has no configured push bot — skip (ADR-0027 §9).
            log.debug(
                "push_team_skip_no_bot",
                message_id=payload.message_id,
                group_id=group_id,
            )
            return

        # Resolve tags + preview with the same helpers as the main dispatcher
        # (ADR-0027 §6). Collapse sibling tags by name (auto-tagging creates one
        # row per team member; the text shows each logical tag once).
        message_tags = await TelegramNotificationsRepo(s).list_tags_for_message(
            message_id=payload.message_id
        )
        seen_tag_names: set[str] = set()
        tag_names: list[str] = []
        for t in message_tags:
            if t.name in seen_tag_names:
                continue
            seen_tag_names.add(t.name)
            tag_names.append(t.name)

        # Body preview: plain-text part, else HTML stripped to plain. Use
        # ``.strip()`` not truthiness — ``body_text`` is NOT NULL with a ``''``
        # server default (notify_service.py:280-284).
        if message.body_text.strip():
            raw_preview = message.body_text
        else:
            raw_preview = html_to_plain(message.body_html)
        body_preview = normalize_preview(raw_preview)

        text_html = format_notification(
            acc_label=account.display_name or account.email,
            from_label=message.from_name or message.from_addr,
            tag_names=tag_names,
            subject=message.subject,
            body_preview=body_preview,
        )

    # Fire-and-forget delivery: NO DB writes, NO reserve/mark_sent, NO recovery
    # or re-enqueue (ADR-0027 §5). Outcomes are only logged.
    # round-42 (ADR-0027 §7): attach the «Посмотреть сообщение» button only
    # when this bot has a webhook_secret — otherwise the callback msg:{id}
    # would have no push-webhook to land on (a hung spinner). Graceful
    # degradation: an unconfigured secret simply sends without the button.
    with_button = bool(bot.webhook_secret)
    for admin_id in settings.admin_telegram_ids:
        outcome = await send_notification(
            chat_id=admin_id,
            text_html=text_html,
            message_id=payload.message_id,
            bot_token=bot.token,
            with_button=with_button,
        )
        if outcome.kind == "ok":
            log.info(
                "push_team_sent",
                message_id=payload.message_id,
                bot=bot.name,
                group_id=group_id,
                chat_id=admin_id,
            )
        elif outcome.kind == "dead":
            # Admin blocked the bot (403). Nothing persisted (no links table);
            # the next message simply retries (ADR-0027 §9).
            log.info(
                "push_team_dead",
                message_id=payload.message_id,
                bot=bot.name,
                chat_id=admin_id,
                detail=(outcome.detail or "")[:200],
            )
        elif outcome.kind == "retry_after":
            log.info(
                "push_team_retry_dropped",
                message_id=payload.message_id,
                bot=bot.name,
                chat_id=admin_id,
                retry_after_sec=outcome.retry_after_sec,
            )
        else:
            # transient / disabled — drop (fire-and-forget, ADR-0027 §5).
            log.info(
                "push_team_transient_dropped",
                message_id=payload.message_id,
                bot=bot.name,
                chat_id=admin_id,
                kind=outcome.kind,
                detail=(outcome.detail or "")[:200],
            )
