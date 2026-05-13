"""Telegram Bot API client + update dispatcher (ADR-0018 + ADR-0022).

Outbound helpers:

- :func:`send_message_with_webapp_button` — POST ``sendMessage`` with an
  inline keyboard that contains a single ``web_app`` button targeting
  ``settings.TELEGRAM_WEBAPP_URL`` (used by ``/start``).
- :func:`send_message` — POST ``sendMessage`` with plain text.
- :func:`send_notification` — POST ``sendMessage`` with parse_mode=HTML and
  a ``callback_data`` inline-button carrying ``msg:{message_id}``. When the
  user taps the button, the webhook receives a :class:`callback_query` and
  the bot replies in-chat with the full email body (bug-fix #5).
  Returns a structured :class:`SendNotificationResult` so the dispatcher
  can act on 403/429/transient separately (ADR-0022 §2.4).
- :func:`answer_callback_query` — POST ``answerCallbackQuery`` to clear
  the spinner on the user's button tap (bug-fix #5).

Inbound dispatcher:

- :func:`handle_update` — parses :class:`TelegramUpdate`, routes ``/start``
  and ``/help`` and silently ignores anything else.

Networking notes (per ADR-0018 + ``docs/06-security.md`` §1.8):

- Use ``httpx.AsyncClient`` with TLS validation on (default; never set
  ``verify=False``). Telegram's API is HTTPS-only at api.telegram.org.
- Per-call ``AsyncClient`` is fine here — we make at most one outbound
  request per webhook hit and there is no real latency win from a shared
  pool at this volume; a process-wide pool would just complicate
  shutdown / forking semantics.
- For webhook-driven outbounds errors are logged at ``warning`` level and
  **swallowed**: the webhook must always return 200 to Telegram so the
  update is dropped from the retry queue.
- For push-notifications (:func:`send_notification`) we return structured
  outcomes — the dispatcher needs to mark dead links, back off on 429
  and retry on transient failures.
- The bot token is never logged: it appears only as part of the URL we
  POST to, and the URL is never passed to logger calls.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import Any, Literal

import httpx

from backend.app.telegram.schemas import TelegramUpdate
from shared.config import get_settings
from shared.logging import get_logger

log = get_logger(__name__)

# Per-call timeout for api.telegram.org (connect + read + write + pool).
# Telegram is generally <500 ms but we leave headroom for transient hops.
_HTTP_TIMEOUT_SECONDS: float = 10.0

# Static labels — kept as module-level constants so they don't bake into
# every call site and are easy to grep.
_WEBAPP_BUTTON_TEXT: str = "Open Mail Aggregator"
_HELP_REPLY: str = "Команды:\n/start — открыть приложение\n/help — справка"
_START_REPLY: str = "Привет! Открой mail-агрегатор:"


def _api_url(method: str) -> str:
    """Build the api.telegram.org endpoint URL for ``method``.

    Caller must have already verified ``settings.telegram_bot_enabled`` —
    here we do not guard, but we do NOT log the resulting URL anywhere
    (it embeds the bot token).
    """
    settings = get_settings()
    return f"https://api.telegram.org/bot{settings.BOT_TOKEN}/{method}"


async def _post_send_message(payload: dict[str, Any]) -> None:
    """POST ``sendMessage`` and absorb any failure as a warning.

    Why swallow: see module docstring — webhook must always return 200.
    """
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as client:
            resp = await client.post(_api_url("sendMessage"), json=payload)
    except httpx.HTTPError as exc:
        # Network-level failure (timeout, DNS, TLS, etc.) — log and move on.
        log.warning(
            "telegram_send_message_network_error",
            chat_id=payload.get("chat_id"),
            error_type=type(exc).__name__,
        )
        return

    if resp.status_code >= 400:
        # Telegram returned an API error (invalid chat, rate-limited by
        # Telegram side, malformed body…). Log status code + a *short*
        # excerpt of the response — never the request URL (token leak).
        log.warning(
            "telegram_send_message_api_error",
            chat_id=payload.get("chat_id"),
            status_code=resp.status_code,
            response_excerpt=resp.text[:200],
        )


async def send_message_with_webapp_button(chat_id: int, text: str) -> None:
    """Send ``text`` to ``chat_id`` with a single inline WebApp button.

    The button label is :data:`_WEBAPP_BUTTON_TEXT`; the URL comes from
    ``settings.TELEGRAM_WEBAPP_URL`` (validated up the stack — caller
    must have checked ``settings.telegram_bot_enabled``).
    """
    settings = get_settings()
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "reply_markup": {
            "inline_keyboard": [
                [
                    {
                        "text": _WEBAPP_BUTTON_TEXT,
                        "web_app": {"url": settings.TELEGRAM_WEBAPP_URL},
                    }
                ]
            ]
        },
    }
    await _post_send_message(payload)


async def send_message(chat_id: int, text: str) -> None:
    """Send a plain-text message (no inline keyboard) to ``chat_id``."""
    await _post_send_message({"chat_id": chat_id, "text": text})


async def send_html_message(chat_id: int, text_html: str) -> None:
    """Send an HTML-formatted message (parse_mode=HTML) to ``chat_id``.

    Used by the callback-query handler (bug-fix #5) to deliver the full
    email body in the chat. Web-page previews are disabled because the
    body often contains URLs we don't want auto-expanded. Network /
    Bot-API failures are absorbed as warnings — the webhook must return
    200 to Telegram regardless.
    """
    await _post_send_message(
        {
            "chat_id": chat_id,
            "text": text_html,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
    )


async def answer_callback_query(
    callback_query_id: str,
    *,
    text: str | None = None,
    show_alert: bool = False,
) -> None:
    """POST ``answerCallbackQuery`` for ``callback_query_id``.

    Telegram requires *every* callback_query to be acknowledged within
    a few seconds — otherwise the user sees a perpetual spinner on the
    button they tapped. ``text`` is optional (None → just dismiss the
    spinner); ``show_alert=True`` pops a modal instead of a transient
    toast (used for error feedback like "session expired").

    Errors are swallowed at warning level for the same reason as
    :func:`_post_send_message` — the webhook must return 200.
    """
    payload: dict[str, Any] = {"callback_query_id": callback_query_id}
    if text:
        # Telegram caps callback-query response text at 200 chars; the
        # client truncates silently if we overshoot.
        payload["text"] = text[:200]
    if show_alert:
        payload["show_alert"] = True
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as client:
            resp = await client.post(_api_url("answerCallbackQuery"), json=payload)
    except httpx.HTTPError as exc:
        log.warning(
            "telegram_answer_callback_network_error",
            callback_query_id=callback_query_id,
            error_type=type(exc).__name__,
        )
        return
    if resp.status_code >= 400:
        log.warning(
            "telegram_answer_callback_api_error",
            callback_query_id=callback_query_id,
            status_code=resp.status_code,
            response_excerpt=resp.text[:200],
        )


# ---------------------------------------------------------------------------
# Push-notifications (ADR-0022 sec. 2.4 - 2.5)
# ---------------------------------------------------------------------------


NotificationKind = Literal["ok", "dead", "retry_after", "transient", "disabled"]


@dataclass(frozen=True, slots=True)
class SendNotificationResult:
    """Structured outcome of :func:`send_notification`.

    ``kind``:

    - ``"ok"``           — sendMessage succeeded; ``telegram_message_id`` set.
    - ``"dead"``         — 403 or 400 — user blocked the bot / chat gone.
                           Dispatcher MUST mark the link as dead.
    - ``"retry_after"``  — 429 rate-limit; dispatcher should sleep
                           ``retry_after_sec`` and retry the same payload.
    - ``"transient"``    — network / 5xx; dispatcher rolls back the
                           ``telegram_notifications`` row and re-enqueues.
    - ``"disabled"``     — bot is not configured (TELEGRAM_BOT_ENABLED=false).
                           Treated as "skip silently"; no audit, no retry.
    """

    kind: NotificationKind
    telegram_message_id: int | None = None
    retry_after_sec: int | None = None
    detail: str | None = None


# HTTP status code constants — named for grep-ability.
_HTTP_TOO_MANY_REQUESTS: int = 429
_HTTP_FORBIDDEN: int = 403
_HTTP_BAD_REQUEST: int = 400
_HTTP_SERVER_ERROR_FLOOR: int = 500


def _is_dead_response_body(body: dict[str, Any]) -> bool:
    """Heuristic: Bot API 400/403 with descriptions that indicate the chat
    is gone / blocked. Bot API returns a JSON like
    ``{ok: false, error_code: 403, description: "Forbidden: bot was blocked by the user"}``.
    """
    description = (body.get("description") or "").lower()
    # Documented strings — defensive list, not exhaustive.
    dead_markers = (
        "bot was blocked",
        "user is deactivated",
        "chat not found",
        "chat_not_found",
        "bot was kicked",
    )
    return any(marker in description for marker in dead_markers)


async def send_notification(  # noqa: PLR0911 - each return is a distinct, documented Bot API outcome
    *,
    chat_id: int,
    text_html: str,
    message_id: int,
) -> SendNotificationResult:
    """POST ``sendMessage`` with HTML parse-mode + an inline WebApp button.

    Returns a :class:`SendNotificationResult`; caller dispatches the
    outcome (see :mod:`worker.app.tg_notify_dispatch`).
    """
    settings = get_settings()
    if not settings.telegram_bot_enabled:
        # Bot disabled (CI / dev). Caller treats this as "skip silently".
        return SendNotificationResult(kind="disabled")

    # Bug-fix #5: the «Посмотреть сообщение» button is now a
    # callback_data button (≤ 64 bytes) — tapping it sends a
    # callback_query to the webhook which replies in-chat with the full
    # message body. Replaces the previous web_app Mini-App opener.
    callback_data = f"msg:{message_id}"

    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text_html,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "reply_markup": {
            "inline_keyboard": [
                [
                    {
                        "text": "Посмотреть сообщение",
                        "callback_data": callback_data,
                    }
                ]
            ]
        },
    }

    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as client:
            resp = await client.post(_api_url("sendMessage"), json=payload)
    except httpx.HTTPError as exc:
        log.warning(
            "telegram_send_notification_network_error",
            chat_id=chat_id,
            message_id=message_id,
            error_type=type(exc).__name__,
        )
        return SendNotificationResult(kind="transient", detail=type(exc).__name__)

    status = resp.status_code

    # 200 OK — usually the happy path; Bot API does sometimes return
    # ``{ok: false}`` in 200 envelopes too, so check both.
    if status < _HTTP_BAD_REQUEST:
        try:
            body = resp.json()
        except ValueError:
            log.warning(
                "telegram_send_notification_non_json_200",
                chat_id=chat_id,
                message_id=message_id,
            )
            return SendNotificationResult(kind="transient", detail="non_json_200")
        if body.get("ok") is True:
            result = body.get("result") or {}
            tg_msg_id_raw = result.get("message_id")
            tg_msg_id = int(tg_msg_id_raw) if isinstance(tg_msg_id_raw, int) else None
            return SendNotificationResult(kind="ok", telegram_message_id=tg_msg_id)
        # 200 with ok=false — translate based on description as for 4xx.
        if _is_dead_response_body(body):
            return SendNotificationResult(
                kind="dead",
                detail=str(body.get("description") or "")[:200],
            )
        log.warning(
            "telegram_send_notification_unexpected_200_body",
            chat_id=chat_id,
            message_id=message_id,
            description_excerpt=str(body.get("description") or "")[:200],
        )
        return SendNotificationResult(
            kind="transient",
            detail=str(body.get("description") or "")[:200],
        )

    # 429: read retry_after from Telegram's response body (or Retry-After header).
    if status == _HTTP_TOO_MANY_REQUESTS:
        retry_after = 1
        try:
            body = resp.json()
            params = body.get("parameters") or {}
            value = params.get("retry_after")
            if isinstance(value, int) and value > 0:
                retry_after = int(value)
        except ValueError:
            pass
        header_value = resp.headers.get("retry-after")
        if header_value:
            with contextlib.suppress(ValueError):
                retry_after = max(retry_after, int(header_value))
        log.info(
            "telegram_send_notification_rate_limited",
            chat_id=chat_id,
            message_id=message_id,
            retry_after_sec=retry_after,
        )
        return SendNotificationResult(kind="retry_after", retry_after_sec=retry_after)

    # 403 / 400 with a "dead" description: user blocked / chat gone.
    if status in (_HTTP_FORBIDDEN, _HTTP_BAD_REQUEST):
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if status == _HTTP_FORBIDDEN or _is_dead_response_body(body):
            log.info(
                "telegram_send_notification_dead_link",
                chat_id=chat_id,
                message_id=message_id,
                status_code=status,
                description_excerpt=str(body.get("description") or "")[:200],
            )
            return SendNotificationResult(
                kind="dead",
                detail=str(body.get("description") or "")[:200],
            )
        # Plain 400 with another reason — treat as transient (likely our bug).
        log.warning(
            "telegram_send_notification_bad_request",
            chat_id=chat_id,
            message_id=message_id,
            description_excerpt=str(body.get("description") or "")[:200],
        )
        return SendNotificationResult(
            kind="transient",
            detail=str(body.get("description") or "")[:200],
        )

    # 5xx: transient by definition.
    if status >= _HTTP_SERVER_ERROR_FLOOR:
        log.warning(
            "telegram_send_notification_server_error",
            chat_id=chat_id,
            message_id=message_id,
            status_code=status,
        )
        return SendNotificationResult(kind="transient", detail=f"http_{status}")

    # Anything else (e.g. 401 — token revoked) → transient; operator must fix.
    log.warning(
        "telegram_send_notification_unexpected_status",
        chat_id=chat_id,
        message_id=message_id,
        status_code=status,
    )
    return SendNotificationResult(kind="transient", detail=f"http_{status}")


async def handle_update(update: TelegramUpdate) -> None:
    """Route a parsed :class:`TelegramUpdate` to the right reply.

    Decision matrix (ADR-0018 §1):

    - ``message`` is ``None`` → no-op (callback_query, edited_message, …).
    - ``message.text`` is ``None`` (e.g. photo) → no-op.
    - ``message.text`` starts with ``/start`` →
      :func:`send_message_with_webapp_button`.
    - ``message.text`` equals ``/help`` (case-sensitive — Telegram lowercases
      commands client-side) → :func:`send_message` with help text.
    - Anything else → silent ignore (bot is not a chatbot).
    """
    if update.message is None:
        return
    text = update.message.text
    if not text:
        return

    chat_id = update.message.chat.id
    # ``startswith("/start")`` covers both bare ``/start`` and the deep-link
    # form ``/start <payload>`` that Telegram sends when a user opens
    # ``t.me/<bot>?start=<payload>``. We do not consume the payload — bot
    # is a launcher, no per-user state.
    if text.startswith("/start"):
        await send_message_with_webapp_button(chat_id, _START_REPLY)
        return
    if text == "/help":
        await send_message(chat_id, _HELP_REPLY)
        return
    # All other inputs: silent no-op.
