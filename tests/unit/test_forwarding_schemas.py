"""Unit tests for the forward-to e-mail validator (ADR-0034 §2).

Source of truth: ``backend/app/forwarding/schemas.py`` — the manual e-mail
pattern (mirror of ``accounts/schemas.py``: exactly one dotted-domain ``@``,
no ``..``, length 3..254). ``EmailStr`` is deliberately unused project-wide.
"""

from __future__ import annotations

import pytest

from backend.app.forwarding.schemas import (
    FORWARD_TO_MAX_LEN,
    ForwardingUpsertRequest,
    ForwardToValidationError,
    validate_forward_to,
)

pytestmark = pytest.mark.unit


class TestValidateForwardToAccepts:
    @pytest.mark.parametrize(
        "raw",
        [
            "leader@example.com",
            "a@b.co",
            "first.last@sub.domain.org",
            "user+tag@example.com",
            "UPPER@Example.COM",
        ],
    )
    def test_valid_addresses_pass(self, raw: str) -> None:
        assert validate_forward_to(raw) == raw

    def test_surrounding_whitespace_is_stripped(self) -> None:
        assert validate_forward_to("  leader@example.com  ") == "leader@example.com"

    def test_min_length_boundary_accepted(self) -> None:
        # 3 chars is the documented lower bound; "a@b" has no dotted domain so
        # pick the shortest dotted-domain address that is still >= 3.
        assert validate_forward_to("a@b.c") == "a@b.c"


class TestValidateForwardToRejects:
    def test_none_is_required_error(self) -> None:
        with pytest.raises(ForwardToValidationError, match="required"):
            validate_forward_to(None)

    @pytest.mark.parametrize(
        "raw",
        [
            "",  # empty
            "no-at-sign.example.com",  # missing @
            "user@nodot",  # domain without a dot
            "user@.com",  # domain starts with a dot
            "user@example.",  # domain ends with a dot
            "user@ex..ample.com",  # consecutive dots in domain
            "@example.com",  # empty local part
        ],
    )
    def test_malformed_addresses_raise(self, raw: str) -> None:
        with pytest.raises(ForwardToValidationError):
            validate_forward_to(raw)

    def test_too_long_address_rejected(self) -> None:
        too_long = "a" * (FORWARD_TO_MAX_LEN) + "@example.com"
        assert len(too_long) > FORWARD_TO_MAX_LEN
        with pytest.raises(ForwardToValidationError, match="length"):
            validate_forward_to(too_long)

    def test_too_short_address_rejected(self) -> None:
        with pytest.raises(ForwardToValidationError, match="length"):
            validate_forward_to("a@")


class TestUpsertRequestSchema:
    def test_defaults_are_none(self) -> None:
        req = ForwardingUpsertRequest.model_validate({})
        assert req.forward_to is None
        assert req.is_active is None

    def test_parses_both_fields(self) -> None:
        req = ForwardingUpsertRequest.model_validate({"forward_to": "x@y.com", "is_active": False})
        assert req.forward_to == "x@y.com"
        assert req.is_active is False
