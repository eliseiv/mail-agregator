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
(U+FEFF) and — round-40 — the bidi formatters LRM (U+200E) and RLM
(U+200F). Without this the inbox shows rows of empty space between
words; the LRM/RLM additions also let the round-39 blank-line collapse
fire on marketing-mail preheader spacers ("\xa0<LRM><RLM>" runs) that
would otherwise be treated as non-blank.

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
# track open rates. They are visually empty but bloat the rendered body
# and Telegram message length budget. Encoded as ``\uXXXX`` escapes so
# the source file stays free of invisible runtime characters (ruff
# PLE2515).
#
# round-40 (ADR-0022 §2.10): the set also covers the two bidi formatters
# U+200E (LRM) and U+200F (RLM). Marketing mail (Glassdoor) builds a
# preheader spacer line by repeating "\xa0<LRM><RLM>". LRM/RLM are
# Unicode category Cf (Format) — they are NOT whitespace, so the
# round-39 collapse class ``[^\S\n]`` does not match them and the spacer
# line is treated as "non-blank" and never collapsed. Stripping them
# here (strip_invisible_padding runs inside sanitize_telegram_html
# BEFORE collapse) turns the spacer into a pure ``\xa0`` run, which the
# round-39 whitespace class then collapses normally. U+00A0 itself is
# deliberately NOT in the set — it is whitespace and is needed for the
# collapse to fire.
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
# NOTE: <br> is NOT in Telegram's whitelist — line breaks must be literal
# "\n". We convert <br>/<br/>/<br /> → "\n" before bleach (see _BR_TO_NL_RE
# in sanitize_telegram_html below).
_TELEGRAM_ALLOWED_TAGS: Final[frozenset[str]] = frozenset(
    {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "a", "code", "pre"}
)
_TELEGRAM_ALLOWED_ATTRS: Final[dict[str, list[str]]] = {
    "a": ["href"],
    "code": ["class"],  # Telegram accepts ``<code class="language-py">``
}
_TELEGRAM_ALLOWED_PROTOCOLS: Final[list[str]] = ["http", "https", "mailto", "tg"]


# --- Tags whose CONTENT must be dropped before bleach (round-13 bug B fix) ---
#
# ``bleach.clean(strip=True)`` removes the *tags* not on the whitelist but
# leaves the **text content** between the opening and closing tag intact.
# For tags like ``<style>``, ``<script>``, ``<head>`` (and friends) the
# "content" is CSS / JavaScript / metadata — the user ends up seeing raw
# ``aepl-item-no-original-price-4col { height: 20px !important; }`` blocks
# in the Telegram preview. We pre-strip those tags **including their inner
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

# Collapse runs of 3+ newlines into a paragraph break. Applied to the
# Telegram-flavour output where compactness matters more than visual
# fidelity. ``re.MULTILINE`` is not needed — we match on ``\n`` runs
# directly.
_COLLAPSE_BLANK_LINES_RE: Final[re.Pattern[str]] = re.compile(r"\n{3,}")

# --- Render-time blank-line normalisation (ADR-0022 §2.10, round-37) -------
#
# When a message body is *displayed* (web ``message_view.html`` / JSON
# ``GET /api/messages/{id}`` / TG "Посмотреть сообщение") Apple / marketing
# mail shows a tall column of blank lines between paragraphs. These helpers
# collapse that artefact at render-time only — the STORED body is untouched
# (tag-matching ``body_contains`` and push-preview keep reading the raw
# value via repo/worker, NOT via ``MessageService.get``).

# Plain-text (``body_text``, the ``<pre>`` branch): a run of 3+ newlines —
# i.e. one or more "blank lines" (a line of optional horizontal whitespace)
# between two real line breaks — collapses to a single paragraph break.
# Uses an explicit horizontal-whitespace class ``[ \t\r\f\v]`` rather than
# ``\s`` so the pattern never consumes the ``\n`` boundaries unpredictably,
# giving a deterministic "at most one blank line" result.
_COLLAPSE_BLANK_TEXT_LINES_RE: Final[re.Pattern[str]] = re.compile(
    r"\n[ \t\r\f\v]*(?:\n[ \t\r\f\v]*)+\n"
)

# HTML (``body_html``, the ``| safe`` branch): empty block-level separators
# (``<p>``/``<div>`` whose content is only whitespace / ``&nbsp;`` / ``<br>``)
# and runs of 3+ ``<br>`` both render as a tall empty column.
_EMPTY_BLOCK_RE: Final[re.Pattern[str]] = re.compile(
    r"<(p|div)\b[^>]*>(?:\s|&nbsp;|<br\s*/?>)*</\1>", re.IGNORECASE
)
_MULTI_BR_RE: Final[re.Pattern[str]] = re.compile(r"(?:<br\s*/?>\s*){3,}", re.IGNORECASE)

# Round-15 fix: Telegram HTML mode rejects <br>; we must convert it to a
# literal newline BEFORE bleach so the line break is preserved as text.
_BR_TO_NL_RE: Final[re.Pattern[str]] = re.compile(r"<\s*br\s*/?\s*>", re.IGNORECASE)
# Block-level closers in marketing HTML often imply a paragraph break;
# we mirror that to "\n" so collapsing tables/divs in the TG view doesn't
# concatenate everything into one wall of text.
_BLOCK_CLOSE_TO_NL_RE: Final[re.Pattern[str]] = re.compile(
    r"</\s*(p|div|tr|li|h[1-6]|blockquote|table)\s*>", re.IGNORECASE
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


def collapse_blank_lines_text(text: str | None) -> str:
    """Collapse runs of 3+ newlines (blank lines between paragraphs) into a
    single paragraph break (``\\n\\n``).

    Render-time normalisation for the plain-text body view (ADR-0022 §2.10).
    Paragraphs stay separated by exactly one blank line; a single blank line
    between paragraphs is preserved untouched (the rule only fires on 3+
    line breaks). Leading / trailing blank lines are stripped.

    Empty / falsy input returns ``""``. The STORED body is never modified —
    this is applied only when assembling the display DTO.
    """
    if not text:
        return ""
    collapsed = _COLLAPSE_BLANK_TEXT_LINES_RE.sub("\n\n", text)
    return collapsed.strip("\n")


def collapse_blank_lines_html(html: str | None) -> str:
    """Collapse empty block separators and ``<br>`` runs in a sanitised HTML
    body so it no longer renders as a tall column of blank lines.

    Render-time normalisation for the HTML body view (ADR-0022 §2.10),
    applied **on top of** :func:`sanitize_email_html` (the markup is already
    whitelisted, so a single regex pass is safe):

    - empty ``<p>``/``<div>`` separators (content only whitespace / ``&nbsp;``
      / ``<br>``) are removed entirely;
    - runs of 3+ ``<br>`` collapse to ``<br><br>``.

    A single pass (not an iterative fixpoint) — nested empty blocks are rare
    in practice and one pass clears the visible column. ``<pre>`` is never
    touched. Empty / falsy input returns ``""``. The STORED body is never
    modified.
    """
    if not html:
        return ""
    collapsed = _EMPTY_BLOCK_RE.sub("", html)  # drop empty <p>/<div>
    return _MULTI_BR_RE.sub("<br><br>", collapsed)  # 3+ <br> → 2


# --- TG full-body post-sanitize collapse (ADR-0022 2.10, round-39) ----------
#
# A "blank" line in the TG full-body view is a line made up of ANY whitespace.
# Unlike round-37 _COLLAPSE_BLANK_TEXT_LINES_RE (narrow ASCII class
# [ \t\r\f\v]) this needs a WIDE class: Apple / marketing indent lines contain
# U+00A0 (nbsp), U+2003 (em space), U+3000 (ideographic space) and friends.
# Zero-width chars (U+200B/200C/200D/2060/FEFF) and — round-40 — the bidi
# formatters LRM (U+200E) / RLM (U+200F) are NOT whitespace in the Unicode
# sense (the [^\S\n] class does NOT match them), but by this point they are
# already removed inside sanitize_telegram_html (strip_invisible_padding, before
# collapse — its codepoint set now also covers LRM/RLM), so they are absent at
# the collapse input. This is what makes marketing-mail preheader spacers
# ("\xa0<LRM><RLM>" runs) arrive here as pure \xa0 whitespace and collapse
# normally. Ordering "collapse AFTER sanitize" is therefore mandatory.
#
# A separator run = (optional whitespace, then a newline OR <br>), repeated so
# that between two "content" lines there are 2+ breaks. Collapse such a run to
# EXACTLY one paragraph separator ("\n\n").
#
# \S in Python re (str mode, re.UNICODE by default) = NON-whitespace, so
# [^\S\n] = "all Unicode whitespace EXCEPT \n" -- includes \xa0 / em space /
# ideographic space, but does NOT eat the \n itself (those are "breaks",
# matched separately via _TG_BREAK). This yields deterministic
# "horizontal-whitespace around breaks". Newline and <br> are normalised as
# interchangeable "breaks".
_TG_BREAK = r"(?:\n|<br\s*/?>)"
_TG_HSPACE = r"[^\S\n]"  # any whitespace EXCEPT \n (incl. \xa0, em/ideographic space)
#
# 3+ breaks (\n|<br>) with arbitrary h-whitespace between/around -> "\n\n".
_COLLAPSE_TG_BLANK_RE: Final[re.Pattern[str]] = re.compile(
    rf"{_TG_HSPACE}*{_TG_BREAK}(?:{_TG_HSPACE}*{_TG_BREAK}){{2,}}{_TG_HSPACE}*"
)
#
# Split on <pre>...</pre>: the capturing group puts <pre> blocks into the ODD
# segments of the re.split result, ordinary text into the EVEN ones. Collapse
# is applied ONLY to the even segments (outside <pre>); newlines inside <pre>
# are significant and preserved verbatim. <pre> is in _TELEGRAM_ALLOWED_TAGS,
# i.e. it survives until collapse -- the split is built RIGHT INTO the function
# body (not a "requirement on backend").
_TG_PRE_SPLIT_RE: Final[re.Pattern[str]] = re.compile(
    r"(<pre\b.*?</pre>)", re.DOTALL | re.IGNORECASE
)


def collapse_blank_lines_tg(text: str | None) -> str:
    """Collapse blank lines in ALREADY-sanitised Telegram HTML.

    Operates on the output of :func:`sanitize_telegram_html` (a mix of "\\n"
    and "<br>"). A run of 3+ line breaks (any combination of "\\n" and "<br>",
    with arbitrary horizontal whitespace -- incl. ``\\xa0``/``\\u2003``/
    ``\\u3000`` -- between them) collapses to a single paragraph separator
    ("\\n\\n"). Paragraphs stay separated by exactly one blank line; a single
    break and a single blank line are left untouched (the rule only fires on
    3+ breaks). Leading / trailing blank lines are stripped.

    ``<pre>`` content is NOT touched: the input is split on ``<pre>...</pre>``
    via ``_TG_PRE_SPLIT_RE`` (the capturing group puts <pre> blocks into the
    ODD segments), collapse is applied ONLY to the even (outside-<pre>)
    segments; newlines inside ``<pre>`` are preserved verbatim. Segments are
    joined back together.

    Applied ONLY in the TG full-body view (``_format_message_body``) on top of
    :func:`sanitize_telegram_html`. Empty / ``None`` input -> ''. The STORED
    body is never modified (render-time only)."""
    if not text:
        return ""
    parts = _TG_PRE_SPLIT_RE.split(text)
    # even segments = text outside <pre> (collapse); odd = <pre>...</pre> (as-is)
    for i in range(0, len(parts), 2):
        parts[i] = _COLLAPSE_TG_BLANK_RE.sub("\n\n", parts[i])
    return "".join(parts).strip("\n")


def sanitize_telegram_html(html: str) -> str:
    """Return HTML reduced to the Telegram Bot API ``parse_mode=HTML`` subset.

    Tags outside the subset are stripped (their text content stays). Used
    by the callback handler when forwarding the full email body to the
    chat: keeps anchor tags clickable while dropping the marketing-email
    chrome (``<table>``, ``<div>``, inline images) Telegram cannot
    render.

    Zero-width padding is also stripped. Multi-blank-line runs that the
    HTML→text reduction often produces are collapsed to a single paragraph
    break so the Telegram preview stays compact (bug fix round-13).
    """
    if not html:
        return ""
    # Round-13 bug B: <style>/<script>/<head>/... bodies must be dropped
    # together with their content. Bleach removes only the tags; for the
    # Telegram subset (which excludes essentially all layout/metadata
    # tags) this would otherwise leak CSS/JS as plain text into the chat.
    prestripped = _prestrip_unsafe_blocks(html)
    # Round-15 fix: replace <br> and block-level closers with literal "\n"
    # BEFORE bleach. Telegram HTML mode does NOT accept <br>, and bleach
    # would otherwise drop these tags silently — collapsing the text to
    # one unreadable line.
    prestripped = _BR_TO_NL_RE.sub("\n", prestripped)
    prestripped = _BLOCK_CLOSE_TO_NL_RE.sub("\n", prestripped)
    cleaned = bleach.clean(
        prestripped,
        tags=_TELEGRAM_ALLOWED_TAGS,
        attributes=_TELEGRAM_ALLOWED_ATTRS,
        protocols=_TELEGRAM_ALLOWED_PROTOCOLS,
        strip=True,
    )
    cleaned = strip_invisible_padding(cleaned)
    # Telegram messages have a 4096-char budget; long marketing emails
    # frequently leave behind dozens of blank lines after the layout
    # tags get stripped. Collapse 3+ consecutive newlines into a
    # paragraph break and trim trailing whitespace so the user sees a
    # compact preview.
    cleaned = _COLLAPSE_BLANK_LINES_RE.sub("\n\n", cleaned)
    return str(cleaned.strip())


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
