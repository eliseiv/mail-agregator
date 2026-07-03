"""APScheduler job: deliver "mailbox auto-disabled" Telegram alerts (ADR-0033 §4).

Tick cadence: ``settings.MAILBOX_ALERT_DISPATCH_INTERVAL_SECONDS`` (default 5s).
``max_instances=1`` + ``coalesce=True`` — see ``worker/app/main.py``. The job is
registered ONLY when ``MAILBOX_DOWN_ALERT_ENABLED=true``.

Producer side: ``worker.sync_cycle._disable_after_failures`` LPUSHes exactly one
item per Active→Disabled transition onto ``mailbox_alert_queue`` (the
``disabled_alert_sent_at`` stamp guarantees one enqueue per transition —
ADR-0033 §2). This module is the consumer.

Per tick:

1. ``LPOP mailbox_alert_queue count=MAILBOX_ALERT_BATCH_SIZE``.
2. For each item:
   - parse; malformed → log + skip (a malformed item never becomes valid).
   - load the account; gone → log + skip (deleted between enqueue and dispatch).
   - resolve recipients via ``list_recipients_for_mailbox`` (same visibility as
     message notifications, minus per-message predicates — ADR-0033 §3).
   - per-chat dedup by ``telegram_user_id``.
   - send the alert with the MAIN bot (no callback button — the alert is not
     about a message). ``403/400`` → mark the link dead (per-chat, ADR-0024).
     ``429/5xx/network`` → log + DROP (fire-and-forget, no re-enqueue — TD-042).

Idempotency is cross-cycle via the ``disabled_alert_sent_at`` stamp (no
per-delivery registry, unlike ``telegram_notifications`` for messages). A
Redis/Bot-API failure never aborts the sync cycle (the LPUSH producer is
try/except-guarded in ``sync_cycle``; this consumer runs under the worker's
``_safe_*`` wrapper).
"""

from __future__ import annotations

import html
import json
from collections.abc import Awaitable
from typing import Any, cast

from shared.config import get_settings
from shared.db import make_session
from shared.logging import get_logger
from shared.redis_client import get_redis

log = get_logger(__name__)

# Redis LIST key shared with the producer (``sync_cycle._enqueue_mailbox_alert``).
MAILBOX_ALERT_QUEUE_KEY = "mailbox_alert_queue"

# ADR-0033 §5: deterministic reason → human-readable RU phrase. The raw
# ``last_sync_error`` is NEVER put in the chat (no host-detail leak — see
# ``06-security.md`` §1.9); only the stable ``reason`` class is mapped.
_REASON_RU: dict[str, str] = {
    "auth_failed": "ошибка авторизации (неверный пароль или логин)",
    "decrypt_fail": "ошибка расшифровки сохранённых учётных данных",
}
_CONSECUTIVE_SUFFIX = "_consecutive_failures"


def _reason_ru(reason: str) -> str:
    """Map a stable ``reason`` to its RU phrase (ADR-0033 §5).

    ``auth_failed`` / ``decrypt_fail`` are looked up directly;
    ``"<N>_consecutive_failures"`` renders the count. Any unrecognised value
    falls back to the generic "server unreachable" phrase (defensive — never
    leaks the raw string).
    """
    mapped = _REASON_RU.get(reason)
    if mapped is not None:
        return mapped
    if reason.endswith(_CONSECUTIVE_SUFFIX):
        n_part = reason[: -len(_CONSECUTIVE_SUFFIX)]
        if n_part.isdigit():
            return f"почтовый сервер недоступен ({n_part} неудачных попыток подряд)"
    return "почтовый сервер недоступен"


def format_mailbox_down(*, acc_label: str, reason: str) -> str:
    """Render the alert text (ADR-0033 §5), ``parse_mode=HTML``.

    ``acc_label`` is user-controlled (``display_name`` / ``email``) → escaped
    with :func:`html.escape`. ``reason`` is a fixed internal class → its mapped
    phrase is a trusted constant, not escaped.
    """
    safe_label = html.escape(acc_label)
    return (
        f"⚠️ Почта <b>{safe_label}</b> не работает: {_reason_ru(reason)}. "
        "Синхронизация приостановлена — проверьте пароль/настройки."
    )


def _parse(raw: str) -> tuple[int, str] | None:
    """Parse a queue item into ``(mail_account_id, reason)`` or ``None``."""
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    mid = data.get("mail_account_id")
    reason = data.get("reason")
    # ``bool`` is an ``int`` subclass — exclude it defensively.
    if not isinstance(mid, int) or isinstance(mid, bool):
        return None
    if not isinstance(reason, str) or not reason:
        return None
    return mid, reason


async def _dispatch_one(raw: str) -> None:
    """Process a single ``mailbox_alert_queue`` item (best-effort delivery)."""
    # Local imports keep the heavy ``backend.app`` graph out of module load.
    from backend.app.repositories.mail_accounts import MailAccountsRepo
    from backend.app.repositories.telegram_notifications import TelegramNotificationsRepo
    from backend.app.telegram.bot import send_notification
    from backend.app.telegram.sso_service import TelegramSSOService

    parsed = _parse(raw)
    if parsed is None:
        log.warning("mailbox_alert_dispatch_malformed", raw_excerpt=raw[:200])
        return
    mail_account_id, reason = parsed

    async with make_session() as s:
        account = await MailAccountsRepo(s).get_by_id(mail_account_id)
        if account is None:
            # Mailbox deleted between enqueue and dispatch — nothing to alert.
            log.info("mailbox_alert_dispatch_account_missing", mail_account_id=mail_account_id)
            return
        acc_label = account.display_name or account.email
        recipients = await TelegramNotificationsRepo(s).list_recipients_for_mailbox(
            mail_account_id=mail_account_id
        )

    if not recipients:
        # No live links / everyone opted out — the disabled state is still
        # visible in the UI (is_active=false + last_sync_error). Skip silently.
        log.info("mailbox_alert_dispatch_no_recipients", mail_account_id=mail_account_id)
        return

    # Per-chat dedup within this alert (multi-link ADR-0024 / owner∩super_admin).
    seen_chats: set[int] = set()
    deduped_chats: list[tuple[int, int]] = []  # (telegram_user_id, user_id)
    for r in recipients:
        if r.telegram_user_id in seen_chats:
            continue
        seen_chats.add(r.telegram_user_id)
        deduped_chats.append((r.telegram_user_id, r.user_id))

    text_html = format_mailbox_down(acc_label=acc_label, reason=reason)

    sent = 0
    dead = 0
    dropped = 0
    for telegram_user_id, user_id in deduped_chats:
        outcome = await send_notification(
            chat_id=telegram_user_id,
            text_html=text_html,
            message_id=None,
            with_button=False,
        )
        if outcome.kind == "ok":
            sent += 1
            log.info(
                "tg_mailbox_alert_sent",
                mail_account_id=mail_account_id,
                user_id=user_id,
            )
        elif outcome.kind == "dead":
            dead += 1
            async with make_session() as s, s.begin():
                await TelegramSSOService(s).mark_link_dead(
                    telegram_user_id=telegram_user_id,
                    user_id=user_id,
                    reason=outcome.detail or "bot_blocked_or_chat_gone",
                )
            log.info(
                "tg_mailbox_alert_dead",
                mail_account_id=mail_account_id,
                user_id=user_id,
                detail=(outcome.detail or "")[:200],
            )
        else:
            # retry_after / transient / disabled — fire-and-forget: drop, do
            # NOT re-enqueue (TD-042). The disabled state stays visible in UI.
            dropped += 1
            log.info(
                "tg_mailbox_alert_dropped",
                mail_account_id=mail_account_id,
                user_id=user_id,
                kind=outcome.kind,
            )

    log.info(
        "mailbox_alert_dispatch_finish",
        mail_account_id=mail_account_id,
        sent=sent,
        dead=dead,
        dropped=dropped,
    )


async def mailbox_alert_dispatch() -> None:
    """One dispatcher tick (ADR-0033 §4)."""
    settings = get_settings()
    redis = get_redis()
    batch_size = settings.MAILBOX_ALERT_BATCH_SIZE

    # ``LPOP key count=N`` returns ``[]`` (not None) for an empty list. redis-py
    # types the awaited result as ``Awaitable[T] | T`` (sync/async union); the
    # runtime here is always async — ``cast`` picks the async branch to satisfy
    # both local and CI mypy.
    raw_items = await cast(
        Awaitable[bytes | str | list[Any] | None],
        redis.lpop(MAILBOX_ALERT_QUEUE_KEY, count=batch_size),
    )
    if not raw_items:
        return

    if isinstance(raw_items, bytes | str):
        # Defensive: some redis clients return a scalar for count=1.
        items: list[str] = [raw_items.decode() if isinstance(raw_items, bytes) else raw_items]
    else:
        items = [(it.decode() if isinstance(it, bytes) else it) for it in raw_items]

    log.info("mailbox_alert_dispatch_start", batch=len(items))

    for raw in items:
        try:
            await _dispatch_one(raw)
        except Exception as exc:
            # Never propagate — one bad item must not skip the rest this tick.
            log.warning(
                "mailbox_alert_dispatch_item_error",
                detail=str(exc)[:200],
                error_type=type(exc).__name__,
            )
