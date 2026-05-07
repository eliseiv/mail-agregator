"""Pydantic schemas for Telegram Bot API ``Update`` payload.

Per ADR-0018 the bot consumes only the minimum fields needed to dispatch
``/start`` / ``/help`` — everything else (callback_query, edited_message,
channel posts, inline_query, …) is ignored via ``extra="ignore"`` so a
forward-compatible Bot API release does not break webhook parsing.

The on-wire field name ``from`` collides with a Python keyword, so the
:class:`TelegramMessage` model uses an alias (``populate_by_name`` so we
can also construct it programmatically with ``from_=``).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class TelegramUser(BaseModel):
    """Subset of Telegram ``User`` we look at (``message.from``).

    Only ``id`` is required by the schema (Bot API guarantees it on every
    user); ``username``/``first_name`` are kept purely for log context and
    are optional.
    """

    id: int
    first_name: str | None = None
    username: str | None = None

    model_config = ConfigDict(extra="ignore")


class TelegramChat(BaseModel):
    """Subset of Telegram ``Chat`` we route on.

    ``id`` is the chat we POST ``sendMessage`` back to.
    """

    id: int
    type: str | None = None

    model_config = ConfigDict(extra="ignore")


class TelegramMessage(BaseModel):
    """Subset of Telegram ``Message``.

    ``from`` is reserved word in Python, hence ``from_`` with alias.
    ``populate_by_name=True`` lets call sites (mainly tests) pass either
    the alias or the python attribute name.
    """

    chat: TelegramChat
    text: str | None = None
    from_: TelegramUser | None = Field(default=None, alias="from")

    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class TelegramUpdate(BaseModel):
    """Top-level Telegram ``Update`` envelope.

    ``message`` is optional: webhooks also fire for ``callback_query``,
    ``edited_message``, channel posts etc., all of which we ignore.
    """

    update_id: int
    message: TelegramMessage | None = None

    model_config = ConfigDict(extra="ignore")
