"""Unit tests for the multi-value address parser used by the send endpoint
and the underlying RFC-light email regex.

Source of truth: ``backend/app/send/router.py`` (split helper) +
``backend/app/send/schemas.py`` (regex + validator).
"""

from __future__ import annotations

import pytest

from backend.app.send.router import _split_addresses
from backend.app.send.schemas import _EMAIL_RE, _validate_addresses

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Splitter — comma / semicolon / whitespace handling
# ---------------------------------------------------------------------------


class TestSplit:
    def test_csv(self) -> None:
        assert _split_addresses("a@x.com,b@x.com,c@x.com") == [
            "a@x.com",
            "b@x.com",
            "c@x.com",
        ]

    def test_semicolon(self) -> None:
        assert _split_addresses("a@x.com;b@x.com;c@x.com") == [
            "a@x.com",
            "b@x.com",
            "c@x.com",
        ]

    def test_mixed_separators(self) -> None:
        assert _split_addresses("a@x.com, b@x.com; c@x.com") == [
            "a@x.com",
            "b@x.com",
            "c@x.com",
        ]

    def test_strips_whitespace_around_each_entry(self) -> None:
        assert _split_addresses("  a@x.com  ,   b@x.com  ") == ["a@x.com", "b@x.com"]

    def test_empty_entries_dropped(self) -> None:
        assert _split_addresses("a@x.com,,b@x.com,") == ["a@x.com", "b@x.com"]
        assert _split_addresses(";;a@x.com;;b@x.com") == ["a@x.com", "b@x.com"]

    def test_empty_input_returns_empty(self) -> None:
        assert _split_addresses("") == []
        assert _split_addresses(None) == []

    def test_whitespace_inside_address_kept(self) -> None:
        # Whitespace is NOT a separator (per the docstring).
        # The whole string is treated as one entry.
        # The address is malformed but the splitter doesn't care; validation
        # happens later.
        assert _split_addresses("a b@x.com") == ["a b@x.com"]


# ---------------------------------------------------------------------------
# Regex — pragmatic email format check
# ---------------------------------------------------------------------------


class TestEmailRegex:
    @pytest.mark.parametrize(
        "addr",
        [
            "user@example.com",
            "user.name+tag@example.co",
            "u@a.bb",
            "_@a.b",
        ],
    )
    def test_regex_accepts_valid(self, addr: str) -> None:
        assert _EMAIL_RE.match(addr) is not None

    @pytest.mark.parametrize(
        "addr",
        [
            "no-at-sign.com",
            "two@@at.com",
            "user@nodot",
            " leading@ws.com",
            "trailing@ws.com ",
            "with space@ws.com",
            "@nolocal.com",
            "nodomain@",
        ],
    )
    def test_regex_rejects_invalid(self, addr: str) -> None:
        assert _EMAIL_RE.match(addr) is None


# ---------------------------------------------------------------------------
# Validator — the wrapper that normalises and rejects
# ---------------------------------------------------------------------------


class TestValidateAddresses:
    def test_strips_whitespace(self) -> None:
        assert _validate_addresses(["  a@b.c  "]) == ["a@b.c"]

    def test_drops_empty_strings(self) -> None:
        assert _validate_addresses(["", "a@b.c", ""]) == ["a@b.c"]

    def test_raises_on_invalid_address(self) -> None:
        with pytest.raises(ValueError, match="invalid email"):
            _validate_addresses(["bogus"])
