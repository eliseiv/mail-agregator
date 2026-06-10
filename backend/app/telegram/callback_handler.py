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
from backend.app.repositories.mail_accounts import MailAccountsRepo
from backend.app.repositories.telegram_links import TelegramLinksRepo
from backend.app.repositories.users import UsersRepo
from backend.app.telegram.bot import (
    answer_callback_query,
    send_html_message,
)
from backend.app.telegram.schemas import TelegramCallbackQuery
from shared.config import PushTeamBot, get_settings
from shared.html_sanitize import (
    collapse_blank_lines_tg,
    linkify_plain_text,
    sanitize_telegram_html,
    strip_invisible_padding,
)
from shared.logging import get_logger
from shared.models import Message

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
    body_html: str | None,
) -> str:
    """Build the HTML-formatted full-body reply (parse_mode=HTML).

    Round-12 bug B: when ``body_html`` is present we run it through
    :func:`shared.html_sanitize.sanitize_telegram_html` — the Bot API
    accepts only ``b/i/u/s/a/code/pre/br``, so the helper strips
    ``<table>``/``<div>``/inline images while preserving anchor tags so
    links stay clickable in the chat. When only ``body_text`` is
    available we still ``linkify`` plain URLs so the user gets clickable
    links instead of raw text.

    Headers (``subject``, ``from_label``) remain ``html.escape``-d — they
    are short and there is no value in linkifying them.
    """
    subject_safe = html.escape(subject) if subject else "<em>(без темы)</em>"
    from_safe = html.escape(from_label)

    if body_html:
        body_safe = sanitize_telegram_html(body_html)
        body_safe = collapse_blank_lines_tg(body_safe)  # round-39: post-sanitize collapse
        if not body_safe.strip():
            # Sanitiser may strip the body to nothing if every tag was
            # disallowed (rare). Fall back to the plain-text path so the
            # user still sees the message content.
            body_safe = ""

    if not body_html or not body_safe.strip():
        if body_text:
            # Strip invisible padding before linkification so we don't
            # bloat the message; ``linkify_plain_text`` escapes for us.
            body_safe = linkify_plain_text(strip_invisible_padding(body_text))
        else:
            body_safe = "<em>(пустое тело)</em>"

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
    # "От" label, fall back to the address. Round-12 bug B: the body now
    # preserves clickable links by routing through the HTML pipeline when
    # available.
    from_label = detail.from_name or detail.from_addr
    text_html = _format_message_body(
        subject=detail.subject,
        from_label=from_label,
        body_text=detail.body_text or "",
        body_html=detail.body_html,
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


async def handle_push_callback_query(
    query: TelegramCallbackQuery,
    bot: PushTeamBot,
    db: AsyncSession,
) -> None:
    """Process a callback_query from a push-only per-team bot (ADR-0027 §11).

    This is a **separate** path from :func:`handle_callback_query`: push bots
    have no ``telegram_links``/visibility model. Authorisation is membership
    in ``settings.admin_telegram_ids`` (from ``.env``) plus a DEFENSIVE
    group-match — the loaded message must belong to ``bot.group_id`` so an
    admin of team X cannot pull a team-Y message by forging ``msg:{id}``
    through bot X's webhook.

    Every reply (body chunks + the final ack) is sent with **this bot's**
    token (``bot.token``), not the main ``BOT_TOKEN``.

    Like the main callback, this never raises — the webhook returns 200 to
    Telegram regardless. The DB is read-only (``Message`` / ``MailAccount``).
    """
    callback_id = query.id
    data = query.data or ""
    settings = get_settings()

    # Step 1: validate the callback_data payload shape (reuse the contract).
    match = _CALLBACK_PATTERN.match(data)
    if match is None:
        log.info(
            "push_callback_invalid_data",
            data_excerpt=data[:64],
            bot=bot.name,
            telegram_user_id=query.from_.id,
        )
        await answer_callback_query(
            callback_id,
            text="Неподдерживаемое действие.",
            show_alert=False,
            bot_token=bot.token,
        )
        return
    message_id = int(match.group(1))

    # Step 2: authorise the tapper — must be a configured push admin. This is
    # the key difference from the main callback: rights = membership in
    # ``admin_telegram_ids``, NOT telegram_links → user → visibility. ``from.id``
    # is signed by Telegram (proven before the webhook).
    telegram_user_id = query.from_.id
    if telegram_user_id not in settings.admin_telegram_ids:
        log.info(
            "push_callback_not_admin",
            bot=bot.name,
            telegram_user_id=telegram_user_id,
            message_id=message_id,
        )
        await answer_callback_query(
            callback_id,
            text="Нет доступа.",
            show_alert=True,
            bot_token=bot.token,
        )
        return

    # Step 3: resolve the chat to reply into. For a private chat with the bot
    # ``message.chat.id`` equals ``from.id``; ``message`` can be absent for very
    # old messages, in which case we can ack but cannot reply.
    if query.message is None or query.message.chat is None:
        log.warning(
            "push_callback_no_chat",
            bot=bot.name,
            telegram_user_id=telegram_user_id,
            message_id=message_id,
        )
        await answer_callback_query(
            callback_id,
            text="Не удалось ответить в чат.",
            bot_token=bot.token,
        )
        return
    chat_id = query.message.chat.id

    # Step 4: load the message + account (unscoped — push uses group-match,
    # not per-user visibility).
    message = await db.get(Message, message_id)
    if message is None:
        log.info(
            "push_callback_message_missing",
            bot=bot.name,
            telegram_user_id=telegram_user_id,
            message_id=message_id,
        )
        await answer_callback_query(
            callback_id,
            text="Сообщение больше не доступно.",
            show_alert=True,
            bot_token=bot.token,
        )
        return

    account = await MailAccountsRepo(db).get_by_id(message.mail_account_id)
    if account is None:
        log.info(
            "push_callback_account_missing",
            bot=bot.name,
            telegram_user_id=telegram_user_id,
            message_id=message_id,
            mail_account_id=message.mail_account_id,
        )
        await answer_callback_query(
            callback_id,
            text="Сообщение больше не доступно.",
            show_alert=True,
            bot_token=bot.token,
        )
        return

    # Step 5: DEFENSIVE group-match — the message must belong to THIS bot's
    # team. Blocks an admin forging ``msg:{id}`` of another team's message
    # through this bot's webhook (ADR-0027 §11).
    if account.group_id != bot.group_id:
        log.info(
            "push_callback_group_mismatch",
            bot=bot.name,
            telegram_user_id=telegram_user_id,
            message_id=message_id,
            bot_group_id=bot.group_id,
            account_group_id=account.group_id,
        )
        await answer_callback_query(
            callback_id,
            text="Сообщение недоступно.",
            show_alert=True,
            bot_token=bot.token,
        )
        return

    # Step 6: build + send the full body using the SAME formatter / splitter
    # as the main callback (round-39/41 sanitize). Reply with this bot's token.
    from_label = message.from_name or message.from_addr
    text_html = _format_message_body(
        subject=message.subject,
        from_label=from_label,
        body_text=message.body_text or "",
        body_html=message.body_html,
    )
    chunks = _split_for_telegram(text_html)
    for chunk in chunks:
        await send_html_message(chat_id, chunk, bot_token=bot.token)

    log.info(
        "push_callback_message_sent",
        bot=bot.name,
        telegram_user_id=telegram_user_id,
        message_id=message_id,
        chunks=len(chunks),
    )

    # Step 7: silent ack (body already delivered) — via this bot.
    await answer_callback_query(callback_id, bot_token=bot.token)
