"""Telegram callback_query handler (bug-fix #5).

When a user taps the «Посмотреть сообщение» button on a push
notification, Telegram fires a ``callback_query`` to the webhook with
``callback_data="msg:<message_id>"``. The handler:

1. Acknowledges the tap (``answerCallbackQuery``) so the user's spinner
   clears.
2. Resolves the Telegram user to an internal ``users`` row via the
   ``telegram_links`` table (rejects dead/missing links).
3. Loads the message under that user's visibility scope. ``ADR-0019``
   visibility rules apply — if the user lost access (e.g. group change)
   the lookup returns 404 and the bot says so politely.
4. Sends the full email body back to the same chat, formatted as HTML
   with ``parse_mode=HTML``. Long bodies are split on line boundaries
   into ``MAX_TELEGRAM_TEXT_LEN``-sized chunks.

Errors are *always* surfaced as a friendly callback-answer (or a chat
message) — the webhook itself returns 200 to Telegram so the update is
dropped from the retry queue.

This module deliberately does not raise; every exit path either
``answerCallbackQuery`` with a brief explanation, or sends a chat
reply + ack. The webhook route absorbs anything we miss.
"""

# Russian UI strings — Cyrillic look-alikes are intentional. ``RUF001``
# matches Cyrillic letters that resemble Latin ones, which Russian
# inherently contains; whole-file allow keeps the noise out of diffs.
# ruff: noqa: RUF001

from __future__ import annotations

import html
import re
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.deps import build_scope
from backend.app.exceptions import NotFoundError
from backend.app.messages.service import MessageService
from backend.app.repositories.telegram_links import TelegramLinksRepo
from backend.app.repositories.users import UsersRepo
from backend.app.telegram.bot import (
    answer_callback_query,
    send_html_message,
)
from backend.app.telegram.schemas import TelegramCallbackQuery
from shared.logging import get_logger

log = get_logger(__name__)

# Telegram caps a single sendMessage payload at 4096 chars (post-HTML).
# ``MAX_TELEGRAM_TEXT_LEN`` is the working budget per chunk — kept well
# under 4096 to leave headroom for the trailing «…» marker and edge
# cases where the splitter cannot find a clean line break. ``MAX_CHUNKS``
# caps the total number of follow-up sendMessage calls so a runaway 1 MB
# body cannot turn into 250 separate Telegram pushes.
MAX_TELEGRAM_TEXT_LEN: Final[int] = 3800
MAX_CHUNKS: Final[int] = 4
_CONTINUATION_MARKER: Final[str] = "\n…"

# Callback-data contract: ``msg:<positive-int>``. Anything else is
# rejected at the regex stage so a forged payload cannot reach the DB.
_CALLBACK_PATTERN: Final[re.Pattern[str]] = re.compile(r"^msg:(\d+)$")


def _format_message_body(
    *,
    subject: str | None,
    from_label: str,
    body_text: str,
) -> str:
    """Build the HTML-formatted full-body reply (parse_mode=HTML).

    Every user-controlled field is :func:`html.escape`-d. The body is
    rendered verbatim (no Markdown / HTML formatting inside) — the
    underlying ``messages.body_text`` is plain text already.
    """
    subject_safe = html.escape(subject) if subject else "<em>(без темы)</em>"
    from_safe = html.escape(from_label)
    body_safe = html.escape(body_text) if body_text else "<em>(пустое тело)</em>"
    return f"<b>Тема:</b> {subject_safe}\n<b>От:</b> {from_safe}\n\n{body_safe}"


def _split_for_telegram(text: str) -> list[str]:
    """Split ``text`` into chunks that fit Telegram's sendMessage limit.

    Splits prefer line boundaries; if a single line is longer than the
    chunk budget we fall back to a hard slice. Caps at :data:`MAX_CHUNKS`
    chunks — the last chunk ends in :data:`_CONTINUATION_MARKER` if the
    body was truncated.

    Returns at least one chunk (possibly empty-bodied messages produce a
    chunk with the headers only — that's still useful context).
    """
    if len(text) <= MAX_TELEGRAM_TEXT_LEN:
        return [text]

    chunks: list[str] = []
    remaining = text
    while remaining and len(chunks) < MAX_CHUNKS:
        if len(remaining) <= MAX_TELEGRAM_TEXT_LEN:
            chunks.append(remaining)
            remaining = ""
            break
        # Find the last newline within the budget — keeps multiline
        # bodies readable.
        cut = remaining.rfind("\n", 0, MAX_TELEGRAM_TEXT_LEN)
        if cut <= 0:
            # No usable newline — hard slice at the budget.
            cut = MAX_TELEGRAM_TEXT_LEN
        chunks.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    if remaining:
        # Truncated — surface the marker so the user knows the message
        # continues on the web app.
        chunks[-1] = chunks[-1].rstrip() + _CONTINUATION_MARKER
    return chunks


async def handle_callback_query(
    query: TelegramCallbackQuery,
    db: AsyncSession,
) -> None:
    """Process a single callback_query update.

    Always acknowledges the tap before returning so the user's spinner
    clears. Logs unexpected errors but never re-raises — the webhook
    must return 200 (see module docstring).
    """
    callback_id = query.id
    data = query.data or ""

    # Step 1: validate the callback_data payload shape.
    match = _CALLBACK_PATTERN.match(data)
    if match is None:
        log.info(
            "telegram_callback_invalid_data",
            data_excerpt=data[:64],
            telegram_user_id=query.from_.id,
        )
        await answer_callback_query(
            callback_id,
            text="Неподдерживаемое действие.",
            show_alert=False,
        )
        return

    try:
        message_id = int(match.group(1))
    except ValueError:
        # Regex already enforced \d+; defensive only.
        await answer_callback_query(callback_id, text="Неверный идентификатор.")
        return

    # Step 2: locate the chat to reply into. ``callback_query.message``
    # can be absent in extremely rare cases (very old messages); in that
    # case we can still ack but cannot reply.
    if query.message is None or query.message.chat is None:
        log.warning(
            "telegram_callback_no_chat",
            telegram_user_id=query.from_.id,
            message_id=message_id,
        )
        await answer_callback_query(callback_id, text="Не удалось ответить в чат.")
        return
    chat_id = query.message.chat.id

    # Step 3: resolve the Telegram user → internal user via telegram_links.
    telegram_user_id = query.from_.id
    link = await TelegramLinksRepo(db).get_by_telegram_user_id(telegram_user_id)
    if link is None or link.dead_at is not None:
        log.info(
            "telegram_callback_no_active_link",
            telegram_user_id=telegram_user_id,
            message_id=message_id,
            has_link=link is not None,
        )
        await answer_callback_query(
            callback_id,
            text="Сессия истекла, откройте бот заново.",
            show_alert=True,
        )
        return

    user = await UsersRepo(db).get_by_id(link.user_id)
    if user is None:
        # Stale link pointing at a deleted user — link cleanup happens in
        # the SSO flow; here we just decline.
        log.warning(
            "telegram_callback_link_user_gone",
            telegram_user_id=telegram_user_id,
            link_user_id=link.user_id,
            message_id=message_id,
        )
        await answer_callback_query(
            callback_id,
            text="Сессия истекла, откройте бот заново.",
            show_alert=True,
        )
        return

    scope = build_scope(user)

    # Step 4: load the message under the resolved visibility scope.
    # ``MessageService.get`` already filters by mail-account visibility
    # so a recipient who lost access (e.g. group change) gets 404.
    try:
        detail = await MessageService(db).get(scope=scope, message_id=message_id)
    except NotFoundError:
        log.info(
            "telegram_callback_message_not_found_or_forbidden",
            telegram_user_id=telegram_user_id,
            user_id=user.id,
            message_id=message_id,
        )
        await answer_callback_query(
            callback_id,
            text="Сообщение больше не доступно.",
            show_alert=True,
        )
        return

    # Step 5: build + send the full body. Prefer ``from_name`` for the
    # "От" label, fall back to the address.
    from_label = detail.from_name or detail.from_addr
    text_html = _format_message_body(
        subject=detail.subject,
        from_label=from_label,
        body_text=detail.body_text or "",
    )
    chunks = _split_for_telegram(text_html)
    for chunk in chunks:
        await send_html_message(chat_id, chunk)

    log.info(
        "telegram_callback_message_sent",
        telegram_user_id=telegram_user_id,
        user_id=user.id,
        message_id=message_id,
        chunks=len(chunks),
    )

    # Step 6: clear the spinner. No text → silent ack (the body is
    # already in the chat as a regular message above).
    await answer_callback_query(callback_id)
