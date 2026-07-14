"""Unit tests for the e-mail address validation used by the outgoing-mail paths.

Source of truth: ``backend/app/send/schemas.py`` (``_EMAIL_RE`` + ``_validate_addresses``)
— the very validator the external send request re-uses for ``to`` / ``cc``
(``backend/app/external/schemas.py::ExternalSendRequest._check_addresses``,
ADR-0048 §1).

ADR-0044 §5 (phase A1/A3): the session compose UI and its ``backend/app/send/router.py``
multi-value splitter (``_split_addresses``) are decommissioned — the CRM sends a JSON
``to``/``cc`` LIST, so there is nothing left to split. The splitter tests went with it;
the regex/validator below stay because they still guard every outgoing address.
"""

from __future__ import annotations

import pytest

from backend.app.send.schemas import _EMAIL_RE, _validate_addresses

pytestmark = pytest.mark.unit


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
