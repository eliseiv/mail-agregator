"""Integration tests for /api/mail-accounts.

Covers JSON + form paths, PATCH/DELETE via method override, sibling delete,
ownership/authz, and validation. IMAP/SMTP test-login is mocked.

Source of truth: ``backend/app/accounts/router.py`` + ``docs/04-api-contracts.md``.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from shared.config import get_settings

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _mock_test_login(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch out the real IMAP/SMTP test-login coroutines used by service.

    Real network would: (a) be flaky in CI; (b) try to connect to
    ``imap.example.invalid`` which would fail with the wrong error type.
    We make both helpers async no-ops so create/update succeeds for the
    success-path tests; individual tests can re-monkeypatch to raise.
    """
    from backend.app.accounts import service as svc_mod

    async def _ok_imap(**_: Any) -> None:
        return None

    async def _ok_smtp(**_: Any) -> None:
        return None

    monkeypatch.setattr(svc_mod, "imap_test_login", _ok_imap)
    monkeypatch.setattr(svc_mod, "smtp_test_login", _ok_smtp)


async def _login(client: httpx.AsyncClient) -> str:
    s = get_settings()
    resp = await client.post(
        "/login",
        data={"username": s.ADMIN_LOGIN, "password": s.ADMIN_PASSWORD},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert resp.status_code == 302
    return resp.cookies["mas_csrf"]


class TestCreateAccount:
    async def test_create_via_json(self, client: httpx.AsyncClient) -> None:
        csrf = await _login(client)
        resp = await client.post(
            "/api/mail-accounts",
            json={
                "email": "user@example.com",
                "password": "p",
                "imap_host": "imap.example.com",
                "imap_port": 993,
                "imap_ssl": True,
                "smtp_host": "smtp.example.com",
                "smtp_port": 465,
                "smtp_ssl": True,
                "smtp_starttls": False,
            },
            headers={"X-CSRF-Token": csrf},
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["email"] == "user@example.com"
        assert "id" in body
        assert "encrypted_password" not in body  # secret never echoed

    async def test_create_via_form_redirects(self, client: httpx.AsyncClient) -> None:
        csrf = await _login(client)
        resp = await client.post(
            "/api/mail-accounts",
            data={
                "csrf_token": csrf,
                "email": "form@example.com",
                "password": "p",
                "imap_host": "imap.example.com",
                "imap_port": "993",
                "imap_ssl": "on",
                "smtp_host": "smtp.example.com",
                "smtp_port": "465",
                "smtp_ssl": "on",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/accounts"

    async def test_duplicate_email_returns_409(self, client: httpx.AsyncClient) -> None:
        csrf = await _login(client)
        body = {
            "email": "dup@example.com",
            "password": "p",
            "imap_host": "imap.example.com",
            "smtp_host": "smtp.example.com",
        }
        a = await client.post(
            "/api/mail-accounts", json=body, headers={"X-CSRF-Token": csrf}
        )
        assert a.status_code == 201
        b = await client.post(
            "/api/mail-accounts", json=body, headers={"X-CSRF-Token": csrf}
        )
        assert b.status_code == 409
        assert b.json()["error"]["code"] == "conflict"

    async def test_imap_failure_returns_422(
        self, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from backend.app.accounts import service as svc_mod
        from backend.app.exceptions import IMAPLoginFailedError

        async def _bad_imap(**_: Any) -> None:
            raise IMAPLoginFailedError("nope")

        monkeypatch.setattr(svc_mod, "imap_test_login", _bad_imap)

        csrf = await _login(client)
        resp = await client.post(
            "/api/mail-accounts",
            json={
                "email": "u@example.com",
                "password": "p",
                "imap_host": "imap.example.com",
                "smtp_host": "smtp.example.com",
            },
            headers={"X-CSRF-Token": csrf},
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "imap_login_failed"


class TestPatchAccount:
    async def test_patch_via_json(self, client: httpx.AsyncClient) -> None:
        csrf = await _login(client)
        a = await client.post(
            "/api/mail-accounts",
            json={
                "email": "u@example.com",
                "password": "p",
                "imap_host": "imap.example.com",
                "smtp_host": "smtp.example.com",
            },
            headers={"X-CSRF-Token": csrf},
        )
        acc_id = a.json()["id"]
        resp = await client.patch(
            f"/api/mail-accounts/{acc_id}",
            json={"smtp_host": "smtp2.example.com"},
            headers={"X-CSRF-Token": csrf},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["smtp_host"] == "smtp2.example.com"

    async def test_patch_via_method_override(
        self, client: httpx.AsyncClient
    ) -> None:
        csrf = await _login(client)
        a = await client.post(
            "/api/mail-accounts",
            json={
                "email": "u@example.com",
                "password": "p",
                "imap_host": "imap.example.com",
                "smtp_host": "smtp.example.com",
            },
            headers={"X-CSRF-Token": csrf},
        )
        acc_id = a.json()["id"]
        # Form POST with _method=PATCH.
        resp = await client.post(
            f"/api/mail-accounts/{acc_id}",
            data={
                "_method": "PATCH",
                "csrf_token": csrf,
                "imap_host": "imap.example.com",
                "imap_port": "993",
                "imap_ssl": "on",
                "smtp_host": "smtp3.example.com",
                "smtp_port": "465",
                "smtp_ssl": "on",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/accounts"


class TestDeleteAccount:
    async def test_delete_via_canonical_method(
        self, client: httpx.AsyncClient
    ) -> None:
        csrf = await _login(client)
        a = await client.post(
            "/api/mail-accounts",
            json={
                "email": "u@example.com",
                "password": "p",
                "imap_host": "imap.example.com",
                "smtp_host": "smtp.example.com",
            },
            headers={"X-CSRF-Token": csrf},
        )
        acc_id = a.json()["id"]
        resp = await client.delete(
            f"/api/mail-accounts/{acc_id}",
            headers={"X-CSRF-Token": csrf},
        )
        assert resp.status_code == 204
        # Already gone -> 404.
        gone = await client.get(f"/api/mail-accounts/{acc_id}")
        assert gone.status_code == 404

    async def test_delete_via_sibling_post_with_method_override(
        self, client: httpx.AsyncClient
    ) -> None:
        csrf = await _login(client)
        a = await client.post(
            "/api/mail-accounts",
            json={
                "email": "sib@example.com",
                "password": "p",
                "imap_host": "imap.example.com",
                "smtp_host": "smtp.example.com",
            },
            headers={"X-CSRF-Token": csrf},
        )
        acc_id = a.json()["id"]

        resp = await client.post(
            f"/api/mail-accounts/{acc_id}/delete",
            data={
                "_method": "DELETE",
                "csrf_token": csrf,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/accounts"

    async def test_post_to_sibling_without_override_returns_405(
        self, client: httpx.AsyncClient
    ) -> None:
        csrf = await _login(client)
        a = await client.post(
            "/api/mail-accounts",
            json={
                "email": "sib2@example.com",
                "password": "p",
                "imap_host": "imap.example.com",
                "smtp_host": "smtp.example.com",
            },
            headers={"X-CSRF-Token": csrf},
        )
        acc_id = a.json()["id"]
        resp = await client.post(
            f"/api/mail-accounts/{acc_id}/delete",
            data={"csrf_token": csrf},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert resp.status_code == 405


class TestUnauthenticated:
    async def test_unauthenticated_get_returns_401_json(
        self, client: httpx.AsyncClient
    ) -> None:
        resp = await client.get("/api/mail-accounts")
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "not_authenticated"


class TestForceSyncMarker:
    async def test_force_sync_writes_redis_key(
        self, client: httpx.AsyncClient, redis_client: Any
    ) -> None:
        csrf = await _login(client)
        a = await client.post(
            "/api/mail-accounts",
            json={
                "email": "fs@example.com",
                "password": "p",
                "imap_host": "imap.example.com",
                "smtp_host": "smtp.example.com",
            },
            headers={"X-CSRF-Token": csrf},
        )
        acc_id = a.json()["id"]

        r = await client.post(
            f"/api/mail-accounts/{acc_id}/sync-now",
            headers={"X-CSRF-Token": csrf},
        )
        assert r.status_code == 202
        # Redis key set.
        val = await redis_client.get(f"force_sync:{acc_id}")
        assert val == "1"
