"""Builtin tags catalogue (ADR-0017 §6).

Source-of-truth for the system tags created lazily for every user on their
first successful login (see
:func:`backend.app.tags.service.TagsService.ensure_builtin_tags`).

Schema mirrors ``docs/03-data-model.md`` "Заполнение builtin-тегов".

round-25: the catalogue was reworked for the App Store Connect workflow.
Each tag now carries a ``match_mode`` (``'any'`` = OR, the default; ``'all'``
= AND — see migration 20260521_015 and ``backend/app/tags/sql.py``). Most of
the new App-Store tags combine a ``sender_contains`` rule (matching the
``App Store Connect`` *display-name*, round-25) with a body/subject rule,
and therefore use ``'all'`` so the tag only attaches to the specific
Apple notification — not to every Apple e-mail.

Colours are reused from the fixed palette in
``backend/app/tags/schemas.py`` (``PALETTE_COLORS``); ``ensure_builtin_tags``
asserts membership defensively.
"""

from __future__ import annotations

from typing import Final, TypedDict


class _BuiltinRule(TypedDict):
    type: str
    pattern: str


class _BuiltinTag(TypedDict):
    name: str
    color: str
    # 'any' (OR, default) or 'all' (AND) — mirrors tags.match_mode.
    match_mode: str
    rules: list[_BuiltinRule]


BUILTIN_TAGS: Final[list[_BuiltinTag]] = [
    # --- Pre-existing tags (preserved) -----------------------------------
    {
        "name": "DPLA.PLA",
        "color": "#2563eb",  # c1 blue
        "match_mode": "any",
        "rules": [
            {"type": "subject_contains", "pattern": "DPLA"},
            {"type": "subject_contains", "pattern": "PLA"},
            {"type": "body_contains", "pattern": "DPLA"},
            {"type": "body_contains", "pattern": "PLA"},
        ],
    },
    {
        # Cancel AND subscription must both appear (round-25: was 'any').
        "name": "Отменить подписку",
        "color": "#f59e0b",  # c3 amber
        "match_mode": "all",
        "rules": [
            {"type": "body_contains", "pattern": "cancel"},
            {"type": "body_contains", "pattern": "subscription"},
        ],
    },
    {
        "name": "Продление аккаунта",
        "color": "#16a34a",  # c4 green
        "match_mode": "any",
        "rules": [
            {
                "type": "body_contains",
                "pattern": "Your Distribution Certificate will no longer be valid in 30 days",
            },
        ],
    },
    # --- App Store Connect workflow (round-25) ---------------------------
    {
        # Dispute notices come from a precise address — exact match only.
        "name": "Диспут",
        "color": "#dc2626",  # c2 red
        "match_mode": "any",
        "rules": [
            {"type": "sender_exact", "pattern": "AppStoreNotices@apple.com"},
        ],
    },
    {
        "name": "Бан Аккаунта",
        "color": "#dc2626",  # c2 red
        "match_mode": "all",
        "rules": [
            {"type": "subject_contains", "pattern": "Notice of Termination"},
            {"type": "sender_contains", "pattern": "Apple Developer"},
        ],
    },
    {
        "name": "Релиз",
        "color": "#16a34a",  # c4 green
        "match_mode": "all",
        "rules": [
            {"type": "sender_contains", "pattern": "App Store Connect"},
            {"type": "body_contains", "pattern": "Congratulations!"},
        ],
    },
    {
        "name": "Реджект",
        "color": "#db2777",  # c7 pink
        "match_mode": "all",
        "rules": [
            {"type": "sender_contains", "pattern": "App Store Connect"},
            {
                "type": "body_contains",
                "pattern": (
                    "We noticed an issue with your submission that requires your attention."
                ),
            },
        ],
    },
    {
        "name": "Ревью",
        "color": "#7c3aed",  # c5 purple
        "match_mode": "all",
        "rules": [
            {"type": "sender_contains", "pattern": "App Store Connect"},
            {"type": "body_contains", "pattern": "In Review"},
        ],
    },
    {
        "name": "Ждет Ревью",
        "color": "#0891b2",  # c6 cyan
        "match_mode": "all",
        "rules": [
            {"type": "sender_contains", "pattern": "App Store Connect"},
            {"type": "body_contains", "pattern": "Waiting for Review"},
        ],
    },
    {
        "name": "Нужна замена реквизитов",
        "color": "#475569",  # c8 slate
        "match_mode": "all",
        "rules": [
            {"type": "sender_contains", "pattern": "App Store Connect"},
            {"type": "subject_contains", "pattern": "Payment Returned"},
        ],
    },
]
