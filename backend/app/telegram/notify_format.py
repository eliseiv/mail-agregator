"""Telegram push-notification text formatter (ADR-0022 §2.5).

Pure-Python (no Jinja2): Telegram's ``parse_mode=HTML`` accepts a tiny
subset of HTML (b, i, u, s, code, pre, a, br…). All user-controlled
strings are escaped via :func:`html.escape` so neither subjects, body
previews nor display names can break the markup.

Round-34 (ADR-0022 §2.5) adds the email **subject** and a short **body
preview** to the push. Both are optional in the output and are escaped /
length-capped here. The two pure helpers that produce a clean plain-text
preview from a message body live in this module too
(:func:`html_to_plain` / :func:`normalize_preview`) — the dispatcher
(``notify_service.dispatch_one_payload``) calls them once per message.
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
#: overhead (ADR-0022 §2.5).
PREVIEW_LEN: Final[int] = 120

#: Maximum number of characters kept from the subject line before it is
#: truncated with an ellipsis. Module constant (see ``PREVIEW_LEN``).
SUBJECT_MAX: Final[int] = 150

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

    ``acc_label`` — mail account ``display_name`` (preferred) or ``email``.
    ``from_label`` — message ``from_name`` (preferred) or ``from_addr``.
    ``tag_names`` — tag names applied to the message; MAY be empty.
    ``subject`` — message subject; ``None``/blank → the subject line is
    omitted. Truncated to :data:`SUBJECT_MAX` characters.
    ``body_preview`` — already normalised + capped preview (see
    :func:`normalize_preview`); ``""`` → the preview line is omitted.

    Line order (ADR-0022 §2.5):

    1. account — **always**;
    2. tag line — only when ``tag_names`` is non-empty (round-31, singular
       vs. plural);
    3. sender — **always**;
    4. ``Тема:`` — only when ``subject`` is non-blank after ``.strip()``
       (round-34); no ``(без темы)`` placeholder in the push;
    5. body preview — only when ``body_preview`` is non-empty (round-34).

    All user-controlled values are escaped via :func:`html.escape`.
    """
    acc_safe = html.escape(acc_label)
    from_safe = html.escape(from_label)
    # Bug-fix #4: Telegram's parse_mode=HTML does NOT decode HTML entities
    # like ``&laquo;`` / ``&raquo;`` / ``&mdash;`` — they ship to the client
    # verbatim and the user sees literal "&laquo;google&raquo;". Use the
    # actual UTF-8 punctuation. The file-level ``# ruff: noqa: RUF001`` keeps
    # ruff from complaining about Cyrillic-look-alike characters.
    lines = [f"Вы получили письмо на почту <b>{acc_safe}</b>"]
    if tag_names:  # optional tag line (round-31)
        if len(tag_names) == 1:
            lines.append(f"Тег «<b>{html.escape(tag_names[0])}</b>»")
        else:
            names = ", ".join(f"«<b>{html.escape(t)}</b>»" for t in tag_names)
            lines.append(f"Теги {names}")
    lines.append(f"Отправитель <b>{from_safe}</b>")
    # Round-34: optional subject line. Folded/multiline headers occasionally
    # carry newlines or runs of whitespace (RFC 2047 decoding) — collapse
    # them to single spaces so the push stays compact (ADR-0022 §2.5
    # edge-cases). Truncate to SUBJECT_MAX on the raw (un-escaped) text so
    # the cut counts visible characters, then escape.
    subj = _WHITESPACE_RUN_RE.sub(" ", strip_invisible_padding(subject or "")).strip()
    if subj:
        if len(subj) > SUBJECT_MAX:
            subj = subj[:SUBJECT_MAX].rstrip() + _ELLIPSIS
        lines.append(f"Тема: <b>{html.escape(subj)}</b>")
    # Round-34: optional body preview line. ``body_preview`` is already
    # normalised + capped by the caller (:func:`normalize_preview`).
    if body_preview:
        lines.append(html.escape(body_preview))
    return "\n".join(lines)
