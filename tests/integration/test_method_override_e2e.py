"""End-to-end checks for the no-JS method override path.

Source of truth: ADR-0015 + ``backend/app/middlewares.py::MethodOverrideMiddleware``.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from shared.config import get_settings

pytestmark = pytest.mark.integration


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


class TestMethodOverrideEndToEnd:
    async def test_post_with_method_delete_via_sibling_succeeds(
        self, client: httpx.AsyncClient
    ) -> None:
        csrf = await _login(client)
        a = await client.post(
            "/api/mail-accounts",
            json={
                "email": "mo@example.com",
                "password": "p",
                "imap_host": "imap.example.com",
                "smtp_host": "smtp.example.com",
            },
            headers={"X-CSRF-Token": csrf},
        )
        acc_id = a.json()["id"]
        resp = await client.post(
            f"/api/mail-accounts/{acc_id}/delete",
            data={"_method": "DELETE", "csrf_token": csrf},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/accounts"

    async def test_method_override_to_unwhitelisted_path_blocked(
        self, client: httpx.AsyncClient
    ) -> None:
        csrf = await _login(client)
        # Attempt to override on /api/admin/audit (not whitelisted).
        resp = await client.post(
            "/api/admin/audit",
            data={"_method": "DELETE", "csrf_token": csrf},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        # The middleware returns 400 method_override_not_allowed BEFORE the
        # CSRF check fires (it's outermost form-aware layer).
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "method_override_not_allowed"

    async def test_invalid_method_value_falls_through(self, client: httpx.AsyncClient) -> None:
        csrf = await _login(client)
        # _method=GET should be ignored — POST is used. Then route mapping
        # gets a regular POST, and POST /api/mail-accounts is the create
        # endpoint (which we hit without proper body) — likely 422.
        resp = await client.post(
            "/api/mail-accounts",
            data={"_method": "GET", "csrf_token": csrf},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        # Either a validation error (route reached as POST) or method-override
        # rejection: both prove the bad override didn't sneak through as GET.
        assert resp.status_code in (400, 422)
