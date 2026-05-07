"""Builtin tags catalogue (ADR-0017 §6).

Source-of-truth for the four system tags created lazily for every user on
their first successful login (see :func:`backend.app.tags.service.TagsService.ensure_builtin_tags`).

Schema mirrors ``docs/03-data-model.md`` "Заполнение builtin-тегов".
"""

from __future__ import annotations

from typing import Final, TypedDict


class _BuiltinRule(TypedDict):
    type: str
    pattern: str


class _BuiltinTag(TypedDict):
    name: str
    color: str
    rules: list[_BuiltinRule]


BUILTIN_TAGS: Final[list[_BuiltinTag]] = [
    {
        "name": "DPLA.PLA",
        "color": "#2563eb",
        "rules": [
            {"type": "subject_contains", "pattern": "DPLA"},
            {"type": "subject_contains", "pattern": "PLA"},
            {"type": "body_contains", "pattern": "DPLA"},
            {"type": "body_contains", "pattern": "PLA"},
        ],
    },
    {
        "name": "Диспут",
        "color": "#dc2626",
        "rules": [
            {"type": "subject_contains", "pattern": "Apple Inc"},
            {"type": "sender_exact", "pattern": "AppStoreNotices@apple.com"},
        ],
    },
    {
        "name": "Отменить подписку",
        "color": "#f59e0b",
        "rules": [
            {"type": "body_contains", "pattern": "cancel"},
            {"type": "body_contains", "pattern": "subscription"},
        ],
    },
    {
        "name": "Продление аккаунта",
        "color": "#16a34a",
        "rules": [
            {
                "type": "body_contains",
                "pattern": "Your Distribution Certificate will no longer be valid in 30 days",
            },
        ],
    },
]
