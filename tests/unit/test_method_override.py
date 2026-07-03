"""Unit tests for MethodOverrideMiddleware whitelist + body re-injection.

Source of truth: ``backend/app/middlewares.py`` + ADR-0015.
"""

from __future__ import annotations

import pytest

from backend.app.middlewares import (
    _ALLOWED_OVERRIDE_METHODS,
    _OVERRIDE_EXACT_PATHS,
    _OVERRIDE_REGEX_PATHS,
    _extract_method_from_form,
    _is_whitelisted_path,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Whitelist
# ---------------------------------------------------------------------------


class TestWhitelist:
    @pytest.mark.parametrize(
        "path",
        [
            "/api/messages/send",
            "/api/mail-accounts",
            "/api/admin/users",
        ],
    )
    def test_exact_paths_match(self, path: str) -> None:
        assert _is_whitelisted_path(path) is True

    @pytest.mark.parametrize(
        "path",
        [
            "/api/mail-accounts/1",
            "/api/mail-accounts/12345",
            "/api/mail-accounts/1/delete",
            "/api/mail-accounts/1/sync-now",
            "/api/admin/users/7/reset",
            "/api/admin/users/7/delete",
        ],
    )
    def test_regex_paths_match(self, path: str) -> None:
        assert _is_whitelisted_path(path) is True

    @pytest.mark.parametrize(
        "path",
        [
            "/api/mail-accounts/abc",  # non-numeric id
            "/api/mail-accounts/1/foo",  # not a known sibling action
            "/api/messages/1",
            "/login",
            "/healthz",
            "/api/admin/audit",
            "/",
        ],
    )
    def test_non_whitelisted_paths_rejected(self, path: str) -> None:
        assert _is_whitelisted_path(path) is False


# ---------------------------------------------------------------------------
# Allowed methods set
# ---------------------------------------------------------------------------


class TestAllowedMethods:
    def test_allowed_set(self) -> None:
        assert frozenset({"DELETE", "PATCH", "PUT"}) == _ALLOWED_OVERRIDE_METHODS

    def test_get_post_not_allowed_as_overrides(self) -> None:
        # Even though they're valid HTTP verbs, _method=GET / POST shouldn't
        # be honored — that would be a request-smuggling-adjacent foot-gun.
        assert "GET" not in _ALLOWED_OVERRIDE_METHODS
        assert "POST" not in _ALLOWED_OVERRIDE_METHODS


# ---------------------------------------------------------------------------
# Form parser
# ---------------------------------------------------------------------------


class TestExtractMethodFromForm:
    def test_extracts_method_from_urlencoded_body(self) -> None:
        body = b"_method=DELETE&csrf_token=abc"
        m = _extract_method_from_form(body, "application/x-www-form-urlencoded")
        assert m == "DELETE"

    def test_method_is_uppercased_and_stripped(self) -> None:
        body = b"_method=%20delete%20"
        m = _extract_method_from_form(body, "application/x-www-form-urlencoded")
        assert m == "DELETE"

    def test_returns_none_when_method_field_missing(self) -> None:
        body = b"csrf_token=abc"
        assert _extract_method_from_form(body, "application/x-www-form-urlencoded") is None

    def test_returns_none_for_non_form_content_type(self) -> None:
        body = b'{"_method":"DELETE"}'
        assert _extract_method_from_form(body, "application/json") is None
        # multipart is also ignored — only plain form-encoded inspected.
        assert _extract_method_from_form(body, "multipart/form-data; boundary=----") is None

    def test_handles_empty_body(self) -> None:
        assert _extract_method_from_form(b"", "application/x-www-form-urlencoded") is None


# ---------------------------------------------------------------------------
# Documentation alignment
# ---------------------------------------------------------------------------


class TestRegexCount:
    def test_regex_paths_present(self) -> None:
        # Per docs/04-api-contracts.md sec. 8 + ADR-0017 (tags) + ADR-0019 (groups)
        # + ADR-0024 (multi-TG) + ADR-0030 (multi-group membership):
        # mail-accounts (PATCH/delete-sibling/sync-now),
        # admin user (PATCH/reset/delete), multi-group membership
        # (POST groups add, DELETE groups sibling — +2 via ADR-0030),
        # groups (PATCH/delete-sibling), tags
        # (PATCH/delete-sibling/rules/rule-delete-sibling/apply), telegram links
        # (delete-sibling). 16 total.
        assert len(_OVERRIDE_REGEX_PATHS) == 16

    def test_telegram_link_delete_path_whitelisted(self) -> None:
        # ADR-0024 §4 — form-fallback DELETE for unlinking a Telegram link (no-JS).
        assert _is_whitelisted_path("/api/telegram/links/1/delete") is True
        assert _is_whitelisted_path("/api/telegram/links/12345/delete") is True
        # Non-numeric id must not match.
        assert _is_whitelisted_path("/api/telegram/links/abc/delete") is False

    def test_exact_paths_present(self) -> None:
        # /api/messages/send, /api/mail-accounts, /api/admin/users, /api/tags,
        # /api/admin/groups (ADR-0019), /api/forwarding/me +
        # /api/forwarding/me/delete (ADR-0034). 7 total.
        assert len(_OVERRIDE_EXACT_PATHS) == 7
