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
    ``tag_names`` — recipient-scoped tag names; rendered in singular vs.
    plural form. Caller is expected to pass a non-empty list (the
    dispatcher refuses to send notifications for messages without any
    recipient-scoped tag — see ADR-0022 §2.2).
    """
    acc_safe = html.escape(acc_label)
    from_safe = html.escape(from_label)
    # Bug-fix #4: Telegram's parse_mode=HTML does NOT decode HTML entities
    # like ``&laquo;`` / ``&raquo;`` / ``&mdash;`` — they ship to the client
    # verbatim and the user sees literal "&laquo;google&raquo;". Use the
    # actual UTF-8 punctuation. The file-level ``# ruff: noqa: RUF001`` keeps
    # ruff from complaining about Cyrillic-look-alike characters.
    if not tag_names:
        # Defensive: format_notification is only called when there is at
        # least one recipient tag (the recipient-resolver guarantees it).
        # Surface a benign placeholder rather than raising — Telegram still
        # delivers a useful message.
        tag_line = "Тег «<b>—</b>»"
    elif len(tag_names) == 1:
        tag_line = f"Тег «<b>{html.escape(tag_names[0])}</b>»"
    else:
        names = ", ".join(f"«<b>{html.escape(t)}</b>»" for t in tag_names)
        tag_line = f"Теги {names}"
    return (
        f"Вы получили письмо на почту <b>{acc_safe}</b>\n"
        f"{tag_line}\n"
        f"Отправитель <b>{from_safe}</b>"
    )
