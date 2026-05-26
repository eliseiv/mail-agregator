"""Telegram push-notification text formatter (ADR-0022 §2.5).

Pure-Python (no Jinja2): Telegram's ``parse_mode=HTML`` accepts a tiny
subset of HTML (b, i, u, s, code, pre, a, br…). All user-controlled
strings are escaped via :func:`html.escape` so neither subjects nor
display names can break the markup.
"""

# Whole-file noqa: the visible strings here are intentional Russian text
# (some characters happen to look like Latin letters but must remain
# Cyrillic to render correctly to end users).
# ruff: noqa: RUF001

from __future__ import annotations

import html


def format_notification(
    *,
    acc_label: str,
    from_label: str,
    tag_names: list[str],
) -> str:
    """Return the HTML body for ``sendMessage`` (parse_mode=HTML).

    ``acc_label`` — mail account ``display_name`` (preferred) or ``email``.
    ``from_label`` — message ``from_name`` (preferred) or ``from_addr``.
    ``tag_names`` — tag names applied to the message; MAY be empty.

    Round-31 (ADR-0022 §2.5): the tag line is **optional**. With
    ``TG_NOTIFY_ALL_MESSAGES`` on (default) a message may arrive without any
    tag; in that case the tag line is omitted entirely (no ``—`` placeholder)
    and the notification is two lines (account + sender). When ``tag_names``
    is non-empty the line is rendered in singular vs. plural form (one vs.
    several tags), yielding three lines.
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
    return "\n".join(lines)
