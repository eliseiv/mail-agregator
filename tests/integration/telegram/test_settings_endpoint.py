"""Integration tests for ``PATCH /api/me/settings`` + ``GET /api/me`` extras
(ADR-0022 §2.7).

Covers:

- PATCH with valid body upserts the row and echoes new state.
- PATCH with empty body → 400 ``validation_error``.
- PATCH with disallowed field → 400 ``validation_error`` (``extra=forbid``).
- GET /api/me exposes ``telegram_linked`` + ``tg_notifications_enabled``.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from shared.config import get_settings

pytestmark = pytest.mark.integration


async def _login_admin(client: httpx.AsyncClient) -> str:
    s = get_settings()
    await client.post(
        "/login",
        data={"username": s.ADMIN_LOGIN},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    resp = await client.post(
        "/login/password",
        data={"password": s.ADMIN_PASSWORD},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    csrf = resp.cookies.get("mas_csrf")
    assert csrf, resp.text
    return csrf


class TestPatchMeSettings:
    async def test_disable_tg_notifications_upserts_and_echoes(
        self, client: httpx.AsyncClient
    ) -> None:
        csrf = await _login_admin(client)
        resp = await client.patch(
            "/api/me/settings",
            json={"tg_notifications_enabled": False},
            headers={"X-CSRF-Token": csrf},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body == {"tg_notifications_enabled": False}

        # GET /api/me reflects the change.
        me = await client.get("/api/me")
        assert me.status_code == 200
        assert me.json()["tg_notifications_enabled"] is False

    async def test_re_enable_tg_notifications(
        self, client: httpx.AsyncClient
    ) -> None:
        csrf = await _login_admin(client)
        # Disable, then re-enable.
        r1 = await client.patch(
            "/api/me/settings",
            json={"tg_notifications_enabled": False},
            headers={"X-CSRF-Token": csrf},
        )
        assert r1.status_code == 200
        r2 = await client.patch(
            "/api/me/settings",
            json={"tg_notifications_enabled": True},
            headers={"X-CSRF-Token": csrf},
        )
        assert r2.status_code == 200
        assert r2.json()["tg_notifications_enabled"] is True

    async def test_empty_body_returns_400_validation_error(
        self, client: httpx.AsyncClient
    ) -> None:
        csrf = await _login_admin(client)
        resp = await client.patch(
            "/api/me/settings",
            json={},
            headers={"X-CSRF-Token": csrf},
        )
        assert resp.status_code == 400, resp.text
        assert resp.json()["error"]["code"] == "validation_error"

    async def test_disallowed_field_returns_400(
        self, client: httpx.AsyncClient
    ) -> None:
        csrf = await _login_admin(client)
        resp = await client.patch(
            "/api/me/settings",
            json={"tg_notifications_enabled": True, "evil": "data"},
            headers={"X-CSRF-Token": csrf},
        )
        # Schema config is ``extra="forbid"`` so unknown fields fail.
        assert resp.status_code == 400, resp.text

    async def test_malformed_json_returns_400(
        self, client: httpx.AsyncClient
    ) -> None:
        csrf = await _login_admin(client)
        resp = await client.patch(
            "/api/me/settings",
            content=b"not-json",
            headers={
                "X-CSRF-Token": csrf,
                "Content-Type": "application/json",
            },
        )
        assert resp.status_code == 400, resp.text
        assert resp.json()["error"]["code"] == "validation_error"


class TestGetMe:
    async def test_get_me_returns_telegram_linked_default_false(
        self, client: httpx.AsyncClient
    ) -> None:
        await _login_admin(client)
        resp = await client.get("/api/me")
        assert resp.status_code == 200
        body = resp.json()
        assert "telegram_linked" in body
        assert body["telegram_linked"] is False
        assert body["tg_notifications_enabled"] is True

    async def test_get_me_telegram_linked_true_after_link(
        self,
        client: httpx.AsyncClient,
        make_link: Any,
        super_admin_user: Any,
    ) -> None:
        await make_link(91999, super_admin_user.id)
        await _login_admin(client)
        resp = await client.get("/api/me")
        assert resp.status_code == 200
        assert resp.json()["telegram_linked"] is True

    async def test_get_me_telegram_linked_false_when_dead(
        self,
        client: httpx.AsyncClient,
        make_link: Any,
        super_admin_user: Any,
    ) -> None:
        await make_link(91998, super_admin_user.id, dead=True)
        await _login_admin(client)
        resp = await client.get("/api/me")
        assert resp.status_code == 200
        assert resp.json()["telegram_linked"] is False
