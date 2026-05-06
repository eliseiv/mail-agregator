"""Integration tests for CSRF middleware: rejects POSTs without/wrong token,
allows when header or form field token matches.

Source of truth: ``backend/app/csrf.py`` + ADR-0010.
"""

from __future__ import annotations

import httpx
import pytest

from shared.config import get_settings

pytestmark = pytest.mark.integration


async def _login(client: httpx.AsyncClient) -> str:
    s = get_settings()
    resp = await client.post(
        "/login",
        data={"username": s.ADMIN_LOGIN, "password": s.ADMIN_PASSWORD},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert resp.status_code == 302
    return resp.cookies["mas_csrf"]


class TestCsrf:
    async def test_post_without_csrf_token_403(self, client: httpx.AsyncClient) -> None:
        await _login(client)
        # POST /api/mail-accounts/test — needs CSRF.
        resp = await client.post(
            "/api/mail-accounts/test",
            json={
                "email": "x@y.com",
                "password": "p",
                "imap_host": "i",
                "smtp_host": "s",
            },
        )
        assert resp.status_code == 403
        body = resp.json()
        assert body["error"]["code"] == "csrf_failed"

    async def test_post_with_wrong_csrf_token_403(self, client: httpx.AsyncClient) -> None:
        await _login(client)
        resp = await client.post(
            "/api/mail-accounts/test",
            json={
                "email": "x@y.com",
                "password": "p",
                "imap_host": "i",
                "smtp_host": "s",
            },
            headers={"X-CSRF-Token": "totally-wrong-token"},
        )
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "csrf_failed"

    async def test_post_with_correct_header_csrf_token_passes(
        self, client: httpx.AsyncClient
    ) -> None:
        csrf = await _login(client)
        # The endpoint will probably 422 because the IMAP/SMTP login fails,
        # but it must NOT be 403 — that proves CSRF accepted the token.
        resp = await client.post(
            "/api/mail-accounts/test",
            json={
                "email": "x@y.com",
                "password": "p",
                "imap_host": "imap.example.invalid",
                "smtp_host": "smtp.example.invalid",
            },
            headers={"X-CSRF-Token": csrf},
        )
        assert resp.status_code != 403, resp.text

    async def test_post_with_form_csrf_token_passes(self, client: httpx.AsyncClient) -> None:
        csrf = await _login(client)
        # Form-encoded POST with csrf_token in body.
        resp = await client.post(
            "/api/messages/send",
            data={
                "csrf_token": csrf,
                "from_account_id": "9999",
                "to": "x@y.com",
                "subject": "x",
                "body": "x",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        # Will fail with 404 (account not found) or similar — but not 403.
        assert resp.status_code != 403, resp.text

    async def test_login_endpoint_exempt(self, client: httpx.AsyncClient) -> None:
        # Login itself never requires CSRF (no session yet).
        s = get_settings()
        resp = await client.post(
            "/login",
            data={"username": s.ADMIN_LOGIN, "password": "wrong"},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        # 401 (bad creds) — NOT 403.
        assert resp.status_code in (401, 423, 429)

    async def test_safe_method_no_csrf_check(self, client: httpx.AsyncClient) -> None:
        await _login(client)
        # GET is safe.
        resp = await client.get("/api/me")
        assert resp.status_code == 200
