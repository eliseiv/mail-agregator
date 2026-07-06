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
helpers that produce a clean plain-text preview from a message body
(:func:`html_to_plain` / :func:`normalize_preview`) now live in
:mod:`shared.preview` and are re-exported here for backward compatibility
— the dispatcher (``notify_service.dispatch_one_payload``) still calls
them once per message via the notify_format names.
"""

# Whole-file noqa: the visible strings here are intentional Russian text
# (some characters happen to look like Latin letters but must remain
# Cyrillic to render correctly to end users).
# ruff: noqa: RUF001

from __future__ import annotations

import html
from typing import Final

from shared.html_sanitize import strip_invisible_padding

# Preview helpers were extracted to ``shared.preview`` so non-Telegram
# callers (the messages inbox listing) can build the same body preview
# without importing this telegram module. Re-exported here for backward
# compatibility — existing importers (``worker.app.push_notify_dispatch``,
# ``backend.app.telegram.notify_service``) keep using the notify_format
# names. ``_WHITESPACE_RUN_RE`` / ``_ELLIPSIS`` are still consumed below by
# :func:`format_notification`.
from shared.preview import (
    _ELLIPSIS,
    _WHITESPACE_RUN_RE,
    PREVIEW_LEN,
    html_to_plain,
    normalize_preview,
)

__all__ = [
    "PREVIEW_LEN",
    "SUBJECT_MAX",
    "format_notification",
    "html_to_plain",
    "normalize_preview",
]

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
