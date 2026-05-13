"""HTML sanitisation for incoming email bodies (round-12 bug B).

Two sanitised flavours live here:

- :func:`sanitize_email_html` — produce safe HTML for the web inbox.
  Whitelist covers the common email-formatting tags (``a``, ``p``,
  ``table``, ``img``, headings…), drops everything else. URLs are
  restricted to ``http``, ``https`` and ``mailto`` — ``javascript:`` and
  ``data:`` are stripped along with every event handler.

- :func:`sanitize_telegram_html` — much tighter whitelist matching the
  Telegram Bot API ``parse_mode=HTML`` subset (``b``, ``i``, ``u``, ``s``,
  ``a``, ``code``, ``pre``). Everything else is stripped (text inside
  preserved). Used by the callback handler so the user receives
  clickable links in chat instead of raw markdown.

Both helpers strip the rampant invisible-padding characters used by
mass-mail engines: zero-width non-joiner (U+200C), zero-width space
(U+200B), zero-width joiner (U+200D), word joiner (U+2060), BOM
(U+FEFF). Without this the inbox shows rows of empty space between
words.

The module is import-light (single bleach import) so the worker and
backend can both pull it in without extra dependency surface.
"""

from __future__ import annotations

from typing import Final

import bleach

# Zero-width / invisible-padding characters that rendering pipelines
# silently keep. Source: Mailchimp, kiwi.com and most ESPs use these to
# defeat clipping ("[Message clipped] View entire message" in Gmail) and
# track open rates. They are visually empty but bloat the rendered body
# and Telegram message length budget. Encoded as ``\uXXXX`` escapes so
# the source file stays free of invisible runtime characters (ruff
# PLE2515).
_INVISIBLE_PADDING_CODEPOINTS: Final[tuple[int, ...]] = (
    0x200B,  # ZERO WIDTH SPACE
    0x200C,  # ZERO WIDTH NON-JOINER
    0x200D,  # ZERO WIDTH JOINER
    0x2060,  # WORD JOINER
    0xFEFF,  # ZERO WIDTH NO-BREAK SPACE / BOM
)
_INVISIBLE_PADDING_TRANSLATE: Final[dict[int, None]] = {
    cp: None for cp in _INVISIBLE_PADDING_CODEPOINTS
}


# Whitelist for the rich web-inbox view. Permissive enough to render
# typical marketing email (tables, headings, inline images), restrictive
# enough to block script execution.
_EMAIL_ALLOWED_TAGS: Final[frozenset[str]] = frozenset(
    {
        "a",
        "br",
        "p",
        "div",
        "span",
        "b",
        "strong",
        "i",
        "em",
        "u",
        "ul",
        "ol",
        "li",
        "blockquote",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "table",
        "thead",
        "tbody",
        "tr",
        "td",
        "th",
        "img",
        "hr",
        "code",
        "pre",
        "sub",
        "sup",
        "small",
    }
)
_EMAIL_ALLOWED_ATTRS: Final[dict[str, list[str]]] = {
    "a": ["href", "title", "target", "rel"],
    "img": ["src", "alt", "title", "width", "height"],
    "td": ["colspan", "rowspan", "align"],
    "th": ["colspan", "rowspan", "align"],
    "span": ["class"],
    "div": ["class"],
    "p": ["class"],
}
_EMAIL_ALLOWED_PROTOCOLS: Final[list[str]] = ["http", "https", "mailto"]


# Telegram Bot API parse_mode=HTML supports exactly this set:
# https://core.telegram.org/bots/api#html-style
_TELEGRAM_ALLOWED_TAGS: Final[frozenset[str]] = frozenset(
    {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "a", "code", "pre", "br"}
)
_TELEGRAM_ALLOWED_ATTRS: Final[dict[str, list[str]]] = {
    "a": ["href"],
    "code": ["class"],  # Telegram accepts ``<code class="language-py">``
}
_TELEGRAM_ALLOWED_PROTOCOLS: Final[list[str]] = ["http", "https", "mailto", "tg"]


def strip_invisible_padding(text: str) -> str:
    """Drop zero-width / invisible padding characters from ``text``.

    These show up as visible empty space in some clients and inflate
    message length without adding any meaning. Stripping is safe — the
    characters never carry semantic content in mail bodies.
    """
    if not text:
        return text
    return text.translate(_INVISIBLE_PADDING_TRANSLATE)


def sanitize_email_html(html: str) -> str:
    """Return a sanitised HTML body safe to render inside the web inbox.

    The output is guaranteed to:

    - contain no ``<script>`` (stripped entirely);
    - contain no inline event handlers (``onclick``, …) — bleach drops
      anything not on the per-tag whitelist;
    - contain no ``javascript:`` / ``data:`` / ``vbscript:`` URLs —
      bleach replaces disallowed protocols with an empty ``href``;
    - have zero-width padding characters removed.

    Empty / falsy input is returned unchanged (``""``).
    """
    if not html:
        return ""
    cleaned = bleach.clean(
        html,
        tags=_EMAIL_ALLOWED_TAGS,
        attributes=_EMAIL_ALLOWED_ATTRS,
        protocols=_EMAIL_ALLOWED_PROTOCOLS,
        strip=True,
    )
    return strip_invisible_padding(cleaned)


def sanitize_telegram_html(html: str) -> str:
    """Return HTML reduced to the Telegram Bot API ``parse_mode=HTML`` subset.

    Tags outside the subset are stripped (their text content stays). Used
    by the callback handler when forwarding the full email body to the
    chat: keeps anchor tags clickable while dropping the marketing-email
    chrome (``<table>``, ``<div>``, inline images) Telegram cannot
    render.

    Zero-width padding is also stripped.
    """
    if not html:
        return ""
    cleaned = bleach.clean(
        html,
        tags=_TELEGRAM_ALLOWED_TAGS,
        attributes=_TELEGRAM_ALLOWED_ATTRS,
        protocols=_TELEGRAM_ALLOWED_PROTOCOLS,
        strip=True,
    )
    return strip_invisible_padding(cleaned)


def linkify_plain_text(text: str) -> str:
    """Wrap bare URLs in ``<a>`` tags. Used as a fallback when only a
    ``text/plain`` body is available.

    ``html.escape`` is applied **before** linkification so user content
    cannot inject markup; ``bleach.linkify`` then converts the escaped
    URL substrings into proper anchor tags.
    """
    if not text:
        return ""
    # bleach.linkify expects pre-escaped HTML (it operates on HTML, not
    # raw text). The helper escapes the input then linkifies.
    import html as _html

    escaped = _html.escape(text)
    linkified: str = bleach.linkify(escaped, parse_email=False)
    return strip_invisible_padding(linkified)
