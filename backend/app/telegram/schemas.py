"""Pydantic schemas for Telegram Bot API ``Update`` payload + ADR-0022
SSO request/response.

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


class TelegramCallbackQuery(BaseModel):
    """Subset of Telegram ``CallbackQuery`` (bug-fix #5).

    A callback_query fires when the user taps an inline-keyboard button
    that has ``callback_data`` set. We need:

    - ``id``    — opaque token to POST back to ``answerCallbackQuery``;
    - ``from_`` — the Telegram User who tapped (we resolve to internal
      user via ``telegram_links`` to enforce visibility);
    - ``data``  — the button's ``callback_data`` payload (we encode it
      as ``"msg:{message_id}"`` — see :func:`send_notification`);
    - ``message.chat.id`` — where to ``sendMessage`` the response.

    Telegram caps ``callback_data`` at 64 bytes, hence the compact key.
    Pydantic ignores other Bot-API fields so this stays forward-compat.
    """

    id: str
    from_: TelegramUser = Field(alias="from")
    message: TelegramMessage | None = None
    data: str | None = None

    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class TelegramUpdate(BaseModel):
    """Top-level Telegram ``Update`` envelope.

    ``message`` and ``callback_query`` are both optional; webhooks also
    fire for ``edited_message`` / channel posts etc. which we still
    ignore. Bug-fix #5 added ``callback_query`` handling.
    """

    update_id: int
    message: TelegramMessage | None = None
    callback_query: TelegramCallbackQuery | None = None

    model_config = ConfigDict(extra="ignore")


# ---------------------------------------------------------------------------
# ADR-0022 — Persistent SSO request / response
# ---------------------------------------------------------------------------


class TelegramAuthRequest(BaseModel):
    """``POST /api/telegram/auth`` body.

    ``init_data`` is the verbatim ``window.Telegram.WebApp.initData``
    string. Length-bound to 4096 chars — well above Telegram's documented
    payload size, but bounded for defence-in-depth.
    """

    init_data: str = Field(min_length=1, max_length=4096)

    model_config = ConfigDict(extra="ignore")


class TelegramAuthResponse(BaseModel):
    """``POST /api/telegram/auth`` body for linked / unlinked / self-heal outcomes.

    - Anonymous SSO (ADR-0022 §1.3): ``{linked, redirect}`` — ``healed`` omitted.
    - Self-heal (round-38, ADR-0022 §1.6): ``{linked: false, healed: true}`` (or
      ``healed: false`` when the best-effort upsert failed internally) — **no**
      ``redirect`` so the frontend does not reload an already-logged-in WebApp.

    ``healed`` defaults to ``None`` and is excluded from the serialised body via
    ``model_dump(exclude_none=True)`` at the call sites, so the existing
    anonymous responses (``linked``/``redirect`` only) are byte-for-byte
    unchanged. ``redirect`` is optional because the self-heal branch omits it.
    """

    linked: bool
    redirect: str | None = None
    healed: bool | None = None

    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# ADR-0024 — multi-link management (POST/GET/DELETE /api/telegram/links)
# ---------------------------------------------------------------------------


class TelegramLinkAddRequest(BaseModel):
    """``POST /api/telegram/links`` body — initData of a fresh TG opened in the
    target Telegram account, to be linked to the active session's user."""

    init_data: str = Field(min_length=1, max_length=4096)

    model_config = ConfigDict(extra="ignore")


class TelegramLinkItem(BaseModel):
    """One entry in ``GET /api/telegram/links``."""

    telegram_user_id: int
    created_at: str  # ISO 8601
    dead: bool

    model_config = ConfigDict(extra="forbid")


class TelegramLinksResponse(BaseModel):
    """``GET /api/telegram/links`` body (ADR-0024 §4)."""

    links: list[TelegramLinkItem]
    max: int

    model_config = ConfigDict(extra="forbid")
