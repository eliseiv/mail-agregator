"""Pydantic schemas for the tags module (ADR-0017).

Source-of-truth for shapes — ``docs/04-api-contracts.md`` section "Tags" +
``docs/05-modules.md`` sec. 17 + ``docs/08-frontend.md`` sec. 5.1.

JSON and form-encoded request bodies share the same Pydantic models; the
router translates form-encoded multi-row ``rule_type[]`` / ``rule_pattern[]``
into the canonical ``rules`` list before validating.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Final, Literal

from pydantic import BaseModel, Field, field_validator

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# 8 fixed colours from ``docs/08-frontend.md`` sec 5.1. Backend rejects any
# colour not in this whitelist (defence-in-depth — the radio form already
# constrains the user, but a direct API call must not bypass).
PALETTE_COLORS: Final[frozenset[str]] = frozenset(
    {
        "#2563eb",  # c1 blue
        "#dc2626",  # c2 red
        "#f59e0b",  # c3 amber
        "#16a34a",  # c4 green
        "#7c3aed",  # c5 purple
        "#0891b2",  # c6 cyan
        "#db2777",  # c7 pink
        "#475569",  # c8 slate
    }
)

ALLOWED_RULE_TYPES: Final[frozenset[str]] = frozenset(
    {"subject_contains", "body_contains", "sender_contains", "sender_exact"}
)

# Hex regex per ``docs/03-data-model.md`` table ``tags`` (CHECK constraint
# is also enforced at the DB layer; we duplicate here so the API surfaces
# a 400 ``validation_error`` before reaching SQL).
_HEX_COLOR_RE: Final[re.Pattern[str]] = re.compile(r"^#[0-9A-Fa-f]{6}$")

RuleType = Literal[
    "subject_contains",
    "body_contains",
    "sender_contains",
    "sender_exact",
]

# Per-tag rule combination mode (migration 20260521_015): ``'any'`` (OR,
# default) attaches the tag when any rule matches; ``'all'`` (AND) only when
# every rule matches. Mirrors the DB CHECK constraint ``ck_tags_match_mode``.
MatchMode = Literal["any", "all"]

ALLOWED_MATCH_MODES: Final[frozenset[str]] = frozenset({"any", "all"})

DEFAULT_MATCH_MODE: Final[str] = "any"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _normalise_color(value: str) -> str:
    """Lower-case the hex digits, validate format + palette membership.

    Stored on disk lower-case to make ``IN`` checks deterministic; the radio
    in the UI also emits lower-case values.
    """
    if not isinstance(value, str):
        raise ValueError("color must be a string")
    if not _HEX_COLOR_RE.match(value):
        raise ValueError("color must be a hex code like #2563eb")
    lowered = "#" + value[1:].lower()
    if lowered not in PALETTE_COLORS:
        raise ValueError("color must be one of the palette values")
    return lowered


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------


class RuleSpec(BaseModel):
    """A single rule as part of a request body (no DB id yet)."""

    type: RuleType
    pattern: Annotated[str, Field(min_length=1, max_length=256)]

    @field_validator("pattern")
    @classmethod
    def _strip_pattern(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("pattern cannot be empty after stripping")
        return stripped


class RuleDTO(BaseModel):
    """Persisted rule shape (response side)."""

    id: int
    type: RuleType
    pattern: str
    created_at: datetime


# ---------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------


class TagBriefDTO(BaseModel):
    """Compact tag representation embedded in messages list/detail."""

    id: int
    name: str
    color: str


class TagDTO(BaseModel):
    """Full tag representation as returned by the tags endpoints."""

    id: int
    name: str
    color: str
    match_mode: MatchMode
    is_builtin: bool
    rules: list[RuleDTO]
    created_at: datetime
    updated_at: datetime


class TagCreateRequest(BaseModel):
    name: Annotated[str, Field(min_length=1, max_length=64)]
    color: str
    # 'any' (OR, default — backward-compatible) or 'all' (AND).
    match_mode: MatchMode = "any"
    # Up to 32 rules per tag — same limit as form-encoded shape (see ADR-0017).
    rules: Annotated[list[RuleSpec], Field(default_factory=list, max_length=32)]
    apply_to_existing: bool = False

    @field_validator("name")
    @classmethod
    def _normalise_name(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("name cannot be empty after stripping")
        return stripped

    @field_validator("color")
    @classmethod
    def _check_color(cls, v: str) -> str:
        return _normalise_color(v)


class TagUpdateRequest(BaseModel):
    """``PATCH /api/tags/{id}`` — partial."""

    name: Annotated[str | None, Field(default=None, min_length=1, max_length=64)] = None
    color: str | None = None
    match_mode: MatchMode | None = None

    @field_validator("name")
    @classmethod
    def _normalise_name(cls, v: str | None) -> str | None:
        if v is None:
            return None
        stripped = v.strip()
        if not stripped:
            raise ValueError("name cannot be empty after stripping")
        return stripped

    @field_validator("color")
    @classmethod
    def _check_color(cls, v: str | None) -> str | None:
        if v is None:
            return None
        return _normalise_color(v)


class TagApplyResult(BaseModel):
    applied_count: int


class TagCreateResult(BaseModel):
    """Response shape for ``POST /api/tags`` — the tag plus apply count."""

    tag: TagDTO
    applied_count: int = 0
