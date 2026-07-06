"""Plain-text body-preview helpers (shared, framework-agnostic).

These two pure helpers turn a raw email body (plain-text or HTML) into a
short, single-line teaser preview:

- :func:`html_to_plain` — strip an HTML body down to readable plain text
  (reusing :mod:`shared.html_sanitize` to drop ``<style>``/``<script>``
  and layout chrome first);
- :func:`normalize_preview` — collapse whitespace, trim and cap the text
  to :data:`PREVIEW_LEN` characters (``…`` suffix on truncation).

Originally these lived in ``backend.app.telegram.notify_format`` (the
Telegram push formatter). They were extracted here so non-Telegram
callers — notably the messages inbox listing — can build the same preview
without importing the telegram module. ``notify_format`` re-exports these
names for backward compatibility.
"""

from __future__ import annotations

import html
import re
from typing import Final

from shared.html_sanitize import sanitize_telegram_html, strip_invisible_padding

#: Maximum number of characters kept from a body preview line. Module
#: constant (NOT env): retuning is unnecessary and an extra flag is pure
#: overhead (ADR-0022 §2.5). Round-36: 120 → 100.
PREVIEW_LEN: Final[int] = 100

#: Horizontal ellipsis appended after a truncation.
_ELLIPSIS: Final[str] = "…"

# Strip any HTML tag left behind after the Telegram-subset sanitiser. The
# sanitiser keeps ``<b>``/``<a>``/``<code>`` etc. — for a *plain* preview
# we want none of that, so we drop every remaining ``<…>`` run.
_ANY_TAG_RE: Final[re.Pattern[str]] = re.compile(r"<[^>]+>")

# Collapse every run of whitespace (incl. newlines, tabs, the non-breaking
# space U+00A0) into a single ASCII space. Invisible zero-width padding is
# removed beforehand via ``strip_invisible_padding`` so it never lingers as
# part of a "whitespace" run.
_WHITESPACE_RUN_RE: Final[re.Pattern[str]] = re.compile(r"[\s\xa0]+")


def html_to_plain(body_html: str | None) -> str:
    """Reduce an HTML email body to clean plain text for a preview.

    ``sanitize_telegram_html`` only narrows the markup to the Telegram
    subset (it *keeps* ``<b>``/``<a>``/``<code>``…) — that is still HTML,
    not plain text. For a teaser preview we strip **all** remaining tags
    and decode HTML entities so the user sees readable text with no markup.

    Empty / ``None`` input returns ``""``.
    """
    if not body_html:
        return ""
    # Reuse the shared sanitiser first: it drops <style>/<script>/<head>
    # bodies (CSS/JS leakage) and converts <br>/block closers to newlines,
    # so we don't carry layout chrome into the preview.
    sanitised = sanitize_telegram_html(body_html)
    # Remove every tag the sanitiser legitimately kept (<b>, <a>, …).
    without_tags = _ANY_TAG_RE.sub(" ", sanitised)
    # Decode entities (``&amp;`` → ``&``, ``&lt;`` → ``<`` …). Callers that
    # feed this into markup (e.g. the Telegram formatter) re-escape it.
    return html.unescape(without_tags)


def normalize_preview(text: str) -> str:
    """Collapse whitespace, trim, and cap ``text`` to :data:`PREVIEW_LEN`.

    - zero-width / invisible padding is removed (reusing
      :func:`shared.html_sanitize.strip_invisible_padding`);
    - every whitespace run (newlines, tabs, ``\\xa0``, multiple spaces) is
      collapsed to a single ASCII space;
    - leading / trailing whitespace is stripped;
    - if the cleaned text is longer than :data:`PREVIEW_LEN` it is cut to
      ``PREVIEW_LEN`` characters (trailing whitespace removed) plus ``…``.

    Returns ``""`` when nothing meaningful remains.
    """
    if not text:
        return ""
    cleaned = strip_invisible_padding(text)
    cleaned = _WHITESPACE_RUN_RE.sub(" ", cleaned).strip()
    if not cleaned:
        return ""
    if len(cleaned) > PREVIEW_LEN:
        return cleaned[:PREVIEW_LEN].rstrip() + _ELLIPSIS
    return cleaned
