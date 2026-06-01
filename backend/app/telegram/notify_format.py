"""Telegram push-notification text formatter (ADR-0022 §2.5).

Pure-Python (no Jinja2): Telegram's ``parse_mode=HTML`` accepts a tiny
subset of HTML (b, i, u, s, code, pre, a, br…). All user-controlled
strings are escaped via :func:`html.escape` so neither subjects, body
previews nor display names can break the markup.

Round-36 (ADR-0022 §2.5) reshapes the push into an emoji-labelled card:
the account (id) and tags header lines are **always** present (tags fall
back to a "no tags" placeholder), the sender line is renamed to "Client"
and the subject line is **always** present (empty subject falls back to a
"no subject" placeholder). Blank-line separators sit between the
header/sender blocks and before the (still optional) body preview. The
preview length cap dropped 120 -> 100 (:data:`PREVIEW_LEN`). The two pure
helpers that produce a clean plain-text preview from a message body live
in this module too (:func:`html_to_plain` / :func:`normalize_preview`) —
the dispatcher (``notify_service.dispatch_one_payload``) calls them once
per message.
"""

# Whole-file noqa: the visible strings here are intentional Russian text
# (some characters happen to look like Latin letters but must remain
# Cyrillic to render correctly to end users).
# ruff: noqa: RUF001

from __future__ import annotations

import html
import re
from typing import Final

from shared.html_sanitize import sanitize_telegram_html, strip_invisible_padding

#: Maximum number of characters kept from the body preview line. Module
#: constant (NOT env): retuning is unnecessary and an extra flag is pure
#: overhead (ADR-0022 §2.5). Round-36: 120 → 100.
PREVIEW_LEN: Final[int] = 100

#: Maximum number of characters kept from the subject line before it is
#: truncated with an ellipsis. Module constant (see ``PREVIEW_LEN``).
SUBJECT_MAX: Final[int] = 150

#: Fallback shown on the ``#️⃣:`` line when the message carries no tags
#: (round-36 — the tag line is now always present, ADR-0022 §2.5).
_NO_TAG: Final[str] = "Не сортировано"

#: Fallback shown on the ``Тема:`` line when the subject is empty/blank
#: (round-36 — the subject line is now always present; matches the
#: callback placeholder §2.6).
_NO_SUBJECT: Final[str] = "(без темы)"

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
    """Reduce an HTML email body to clean plain text for a push preview.

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
    # Decode entities (``&amp;`` → ``&``, ``&lt;`` → ``<`` …). The result is
    # re-escaped by ``format_notification`` before it reaches Telegram.
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

    Returns ``""`` when nothing meaningful remains (the preview line is
    then omitted by :func:`format_notification`).
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


def format_notification(
    *,
    acc_label: str,
    from_label: str,
    tag_names: list[str],
    subject: str | None,
    body_preview: str,
) -> str:
    """Return the HTML body for ``sendMessage`` (parse_mode=HTML).

    ``acc_label`` — mail account ``display_name`` (preferred) or ``email``
    (the account nickname on the ``id`` header line).
    ``from_label`` — message ``from_name`` (preferred) or ``from_addr``
    (the client on the ``Client`` line).
    ``tag_names`` — tag names applied to the message (deduped by
    ``(name, color)`` in ``notify_service``); MAY be empty → ``_NO_TAG``.
    ``subject`` — message subject; ``None``/blank after ``.strip()`` →
    ``_NO_SUBJECT``. Truncated to :data:`SUBJECT_MAX` characters.
    ``body_preview`` — already normalised + capped preview (see
    :func:`normalize_preview`); ``""`` → the preview line **and** the
    preceding blank separator are omitted (no trailing empty line).

    Structure (ADR-0022 §2.5, round-36) — 6 lines, 2 blank separators:

    1. ``id`` header — **always** (the account nickname);
    2. ``tags`` header — **always** (all tags joined by ``", "`` or
       :data:`_NO_TAG`);
    3. blank separator;
    4. ``Client`` line — **always** (the sender);
    5. ``Subject`` line — **always** (empty subject → :data:`_NO_SUBJECT`);
    6. blank separator — only when a body preview follows;
    7. body preview — only when ``body_preview`` is non-empty.

    The emoji labels are plain UTF-8; the values are wrapped in ``<b>``.
    All user-controlled values are escaped via :func:`html.escape`.
    """
    # Bug-fix #4: Telegram's parse_mode=HTML does NOT decode HTML entities
    # like ``&laquo;`` / ``&raquo;`` / ``&mdash;`` — they ship to the client
    # verbatim and the user sees literal "&laquo;google&raquo;". Use the
    # actual UTF-8 punctuation. The file-level ``# ruff: noqa: RUF001`` keeps
    # ruff from complaining about Cyrillic-look-alike characters.
    acc_safe = html.escape(acc_label)
    from_safe = html.escape(from_label)
    # Tags header: all tags joined by ", " (dedup by (name, color) done in
    # notify_service), or the _NO_TAG fallback when the message has no tags.
    tags_safe = ", ".join(html.escape(t) for t in tag_names) if tag_names else html.escape(_NO_TAG)
    # Subject line: always present; empty -> _NO_SUBJECT; otherwise collapse
    # folded/multiline header whitespace (RFC 2047 decoding) to single spaces
    # and truncate to SUBJECT_MAX on the raw (un-escaped) text so the cut
    # counts visible characters, then escape.
    subj = _WHITESPACE_RUN_RE.sub(" ", strip_invisible_padding(subject or "")).strip()
    if not subj:
        subj = _NO_SUBJECT
    elif len(subj) > SUBJECT_MAX:
        subj = subj[:SUBJECT_MAX].rstrip() + _ELLIPSIS
    subj_safe = html.escape(subj)
    lines = [
        f"🆔: <b>{acc_safe}</b>",
        f"#️⃣: <b>{tags_safe}</b>",
        "",  # blank separator
        f"Клиент: <b>{from_safe}</b>",
        f"Тема: <b>{subj_safe}</b>",
    ]
    # Round-36: optional body preview line. ``body_preview`` is already
    # normalised + capped (PREVIEW_LEN) by the caller (:func:`normalize_preview`).
    # When absent, the preceding blank separator is omitted too.
    if body_preview:
        lines.append("")  # blank separator before the preview
        lines.append(html.escape(body_preview))
    return "\n".join(lines)
