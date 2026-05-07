"""Telegram Bot API client + update dispatcher (ADR-0018).

Two outbound helpers:

- :func:`send_message_with_webapp_button` — POST ``sendMessage`` with an
  inline keyboard that contains a single ``web_app`` button targeting
  ``settings.TELEGRAM_WEBAPP_URL``.
- :func:`send_message` — POST ``sendMessage`` with plain text (no keyboard).

One inbound dispatcher:

- :func:`handle_update` — parses :class:`TelegramUpdate`, routes ``/start``
  and ``/help`` and silently ignores anything else.

Networking notes (per ADR-0018 + ``docs/06-security.md`` §1.8):

- Use ``httpx.AsyncClient`` with TLS validation on (default; never set
  ``verify=False``). Telegram's API is HTTPS-only at api.telegram.org.
- Per-call ``AsyncClient`` is fine here — we make at most one outbound
  request per webhook hit and there is no real latency win from a shared
  pool at this volume; a process-wide pool would just complicate
  shutdown / forking semantics.
- Errors are logged at ``warning`` level and **swallowed**: the webhook
  must always return 200 to Telegram so the update is dropped from the
  retry queue (Telegram replays for hours on non-2xx). The user can
  always re-send ``/start`` if the first attempt did not produce a reply.
- The bot token is never logged: it appears only as part of the URL we
  POST to, and the URL is never passed to logger calls.
"""

from __future__ import annotations

from typing import Any

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
