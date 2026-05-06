"""Unit tests for CSRF token comparison and form-body parsing.

Source of truth: ``backend/app/csrf.py`` + ADR-0010.
"""

from __future__ import annotations

import secrets

import pytest

from backend.app.csrf import _extract_token_from_form

pytestmark = pytest.mark.unit


class TestCompareDigest:
    """The middleware uses ``secrets.compare_digest`` — verify the contract."""

    def test_compare_digest_returns_true_on_equal(self) -> None:
        a = "abc123" * 4
        b = "abc123" * 4
        assert secrets.compare_digest(a, b) is True

    def test_compare_digest_returns_false_on_differ(self) -> None:
        assert secrets.compare_digest("abc", "xyz") is False

    def test_compare_digest_handles_different_lengths(self) -> None:
        # Different length must NOT raise; just return False.
        assert secrets.compare_digest("a", "ab") is False


class TestExtractTokenFromForm:
    def test_extracts_csrf_token_from_urlencoded_body(self) -> None:
        body = b"username=alice&csrf_token=abc123&password=p"
        token = _extract_token_from_form(body, "application/x-www-form-urlencoded")
        assert token == "abc123"

    def test_returns_none_for_non_form_content_type(self) -> None:
        body = b'{"csrf_token":"abc"}'
        assert _extract_token_from_form(body, "application/json") is None
        assert _extract_token_from_form(body, "multipart/form-data; boundary=---") is None

    def test_returns_none_when_field_missing(self) -> None:
        body = b"username=alice&password=p"
        assert _extract_token_from_form(body, "application/x-www-form-urlencoded") is None

    def test_handles_url_encoded_characters(self) -> None:
        # ``%20`` is a space, ``%2B`` is +
        body = b"csrf_token=token%20with%2Bplus"
        token = _extract_token_from_form(body, "application/x-www-form-urlencoded")
        assert token == "token with+plus"

    def test_handles_empty_body(self) -> None:
        assert _extract_token_from_form(b"", "application/x-www-form-urlencoded") is None

    def test_content_type_match_is_case_insensitive(self) -> None:
        body = b"csrf_token=t"
        assert (
            _extract_token_from_form(body, "Application/X-WWW-Form-URLEncoded") == "t"
        )

    def test_first_csrf_token_wins(self) -> None:
        body = b"csrf_token=first&csrf_token=second"
        # urlencoded with duplicate keys — implementation takes the first one.
        # (Per current code: it iterates pairs and returns on first match.)
        assert (
            _extract_token_from_form(body, "application/x-www-form-urlencoded") == "first"
        )

    def test_charset_in_content_type_still_matches(self) -> None:
        body = b"csrf_token=abc"
        token = _extract_token_from_form(
            body, "application/x-www-form-urlencoded; charset=utf-8"
        )
        assert token == "abc"
