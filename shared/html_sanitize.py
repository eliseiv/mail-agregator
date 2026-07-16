"""HTML sanitisation for incoming email bodies (round-12 bug B).

:func:`sanitize_email_html` produces a safe HTML body. The whitelist covers
the common email-formatting tags (``a``, ``p``, ``table``, ``img``,
headings…) and drops everything else. URLs are restricted to ``http``,
``https`` and ``mailto`` — ``javascript:`` and ``data:`` are stripped along
with every event handler. The connector stores the result and pushes it to
the CRM (ADR-0043 §2), which renders it.

:func:`strip_invisible_padding` removes the rampant invisible-padding
characters used by mass-mail engines: zero-width non-joiner (U+200C),
zero-width space (U+200B), zero-width joiner (U+200D), word joiner
(U+2060), BOM (U+FEFF) and — round-40 — the bidi formatters LRM (U+200E)
and RLM (U+200F). Without this a rendered body shows rows of empty space
between words.

Removed with the decommission (ADR-0044 A3, TD-060): the Telegram flavour
(``sanitize_telegram_html`` + its ``collapse_blank_lines_tg`` post-pass) and
the render-time blank-line normalisers ``collapse_blank_lines_text`` /
``collapse_blank_lines_html`` (ADR-0022 §2.10) — the Telegram bot and the
Jinja inbox that consumed them are gone, and ``linkify_plain_text`` with
them. The connector pushes raw bodies; display normalisation belongs to the
CRM.

The module is import-light (single bleach import) so the worker and
backend can both pull it in without extra dependency surface.
"""

from __future__ import annotations

import re
from typing import Final

import bleach

# Zero-width / invisible-padding characters that rendering pipelines
# silently keep. Source: Mailchimp, kiwi.com and most ESPs use these to
# defeat clipping ("[Message clipped] View entire message" in Gmail) and
# track open rates. They are visually empty but bloat the stored body.
# Encoded as ``\uXXXX`` escapes so the source file stays free of invisible
# runtime characters (ruff PLE2515).
#
# round-40 (ADR-0022 §2.10): the set also covers the two bidi formatters
# U+200E (LRM) and U+200F (RLM). Marketing mail (Glassdoor) builds a
# preheader spacer line by repeating "\xa0<LRM><RLM>"; LRM/RLM are Unicode
# category Cf (Format), not whitespace, so a spacer line reads as "non-blank"
# to any whitespace-based normaliser downstream. Stripping them here turns
# the spacer into a pure ``\xa0`` run. U+00A0 itself is deliberately NOT in
# the set — it is whitespace and carries meaning in the body.
_INVISIBLE_PADDING_CODEPOINTS: Final[tuple[int, ...]] = (
    0x200B,  # ZERO WIDTH SPACE
    0x200C,  # ZERO WIDTH NON-JOINER
    0x200D,  # ZERO WIDTH JOINER
    0x200E,  # LEFT-TO-RIGHT MARK (round-40: marketing preheader spacer)
    0x200F,  # RIGHT-TO-LEFT MARK (round-40: marketing preheader spacer)
    0x2060,  # WORD JOINER
    0xFEFF,  # ZERO WIDTH NO-BREAK SPACE / BOM
)
_INVISIBLE_PADDING_TRANSLATE: Final[dict[int, None]] = {
    cp: None for cp in _INVISIBLE_PADDING_CODEPOINTS
}


# Whitelist for the stored/pushed rich body. Permissive enough to keep
# typical marketing email renderable (tables, headings, inline images),
# restrictive enough to block script execution.
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


# --- Tags whose CONTENT must be dropped before bleach (round-13 bug B fix) ---
#
# ``bleach.clean(strip=True)`` removes the *tags* not on the whitelist but
# leaves the **text content** between the opening and closing tag intact.
# For tags like ``<style>``, ``<script>``, ``<head>`` (and friends) the
# "content" is CSS / JavaScript / metadata — the user ends up seeing raw
# ``aepl-item-no-original-price-4col { height: 20px !important; }`` blocks
# in the rendered body. We pre-strip those tags **including their inner
# content** before handing the body to bleach.
#
# Self-closing / void elements (``<meta>``, ``<link>``, ``<base>``) don't
# have closing tags; they still leak attributes that bleach would keep
# stripping, but they don't carry text content. We drop them here for
# cheap predictability.
#
# HTML comments are also dropped — they frequently contain Outlook
# conditional comments (``<!--[if mso]>...<![endif]-->``) or tracking
# pixels we do not want to leak into the rendered body.
_DROP_TAG_CONTENT_RE: Final[re.Pattern[str]] = re.compile(
    r"<\s*(style|script|head|title|meta|link|noscript|svg|canvas|iframe|"
    r"object|embed|form|input|button|select|textarea)\b[^>]*>.*?"
    r"<\s*/\s*\1\s*>",
    re.IGNORECASE | re.DOTALL,
)
_DROP_SELF_CLOSING_RE: Final[re.Pattern[str]] = re.compile(
    r"<\s*(meta|link|base)\b[^>]*?/?>",
    re.IGNORECASE,
)
_DROP_HTML_COMMENT_RE: Final[re.Pattern[str]] = re.compile(
    r"<!--.*?-->",
    re.DOTALL,
)


def _prestrip_unsafe_blocks(html: str) -> str:
    """Drop the *content* of style/script/etc. blocks before bleach.

    ``bleach.clean(strip=True)`` only removes the wrapping tags — for
    ``<style>`` and ``<script>`` that leaves the CSS / JS body inline as
    plain text. We must remove the entire block (open tag + content +
    close tag) before invoking bleach.

    Also removes HTML comments and self-closing void elements (``<meta>``,
    ``<link>``, ``<base>``) that bleach would otherwise leave behind as
    empty markers.

    The function is intentionally idempotent — running it twice produces
    the same result, which simplifies reasoning at call sites.
    """
    if not html:
        return html
    cleaned = _DROP_TAG_CONTENT_RE.sub("", html)
    cleaned = _DROP_HTML_COMMENT_RE.sub("", cleaned)
    return _DROP_SELF_CLOSING_RE.sub("", cleaned)


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
    """Return a sanitised HTML body safe to store, push and render.

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
    # Round-13 bug B: pre-strip <style>/<script>/<head>/... blocks *with*
    # their text content before bleach. Otherwise the user sees raw CSS /
    # JS dumped into the rendered body (bleach removes tags only, not the
    # text inside them).
    prestripped = _prestrip_unsafe_blocks(html)
    cleaned = bleach.clean(
        prestripped,
        tags=_EMAIL_ALLOWED_TAGS,
        attributes=_EMAIL_ALLOWED_ATTRS,
        protocols=_EMAIL_ALLOWED_PROTOCOLS,
        strip=True,
    )
    return strip_invisible_padding(cleaned)
