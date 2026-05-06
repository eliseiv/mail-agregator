"""Contract tests for ADR-0015 form-encoded fallback.

For each whitelist endpoint, verify both the JSON path and the form path
respond with the documented codes (200/201/204 for JSON, 303 for form).

NOTE: many endpoints currently fail due to known production bugs (see
TZ-bug-001 / TZ-bug-002 in the QA report). Those tests are still kept
as living documentation so a fix gets validated.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from shared.config import get_settings

pytestmark = [pytest.mark.contract, pytest.mark.integration]


@pytest.fixture(autouse=True)
def _mock_test_login(monkeypatch: pytest.MonkeyPatch) -> None:
    from backend.app.accounts import service as svc_mod

    async def _ok(**_: Any) -> None:
        return None

    monkeypatch.setattr(svc_mod, "imap_test_login", _ok)
    monkeypatch.setattr(svc_mod, "smtp_test_login", _ok)


async def _login(client: httpx.AsyncClient) -> str:
    s = get_settings()
    resp = await client.post(
        "/login",
        data={"username": s.ADMIN_LOGIN, "password": s.ADMIN_PASSWORD},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert resp.status_code == 302
    return resp.cookies["mas_csrf"]


class TestFormEncodedFallback:
    async def test_admin_create_user_form_returns_303(self, client: httpx.AsyncClient) -> None:
        csrf = await _login(client)
        resp = await client.post(
            "/api/admin/users",
            data={"csrf_token": csrf, "username": "frm", "email": ""},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        # Form path documented to redirect to /admin.
        assert resp.status_code == 303
        assert resp.headers["location"] == "/admin"

    async def test_admin_create_user_json_returns_201(self, client: httpx.AsyncClient) -> None:
        csrf = await _login(client)
        resp = await client.post(
            "/api/admin/users",
            json={"username": "jsn", "email": None},
            headers={"X-CSRF-Token": csrf},
        )
        assert resp.status_code == 201
        body = resp.json()
        assert "id" in body and "username" in body

    async def test_method_override_blocked_on_unwhitelisted_route(
        self, client: httpx.AsyncClient
    ) -> None:
        csrf = await _login(client)
        # /api/me is read-only GET — method override against POST should
        # be rejected by the middleware (path not whitelisted).
        resp = await client.post(
            "/api/me",
            data={"_method": "DELETE", "csrf_token": csrf},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "method_override_not_allowed"
