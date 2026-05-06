"""End-to-end check: form POST -> 303 -> follow redirect -> flash visible.

Source of truth: ``backend/app/flash.py`` + ADR-0015.
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


class TestFlashEndToEnd:
    async def test_create_account_form_then_get_accounts_shows_flash(
        self, client: httpx.AsyncClient
    ) -> None:
        csrf = await _login(client)
        # Form-encoded POST that flashes "Email-аккаунт добавлен".
        post = await client.post(
            "/api/mail-accounts",
            data={
                "csrf_token": csrf,
                "email": "flash@example.com",
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
        assert post.status_code == 303
        # Now GET /accounts — the flash must appear, then be cleared.
        get1 = await client.get("/accounts")
        assert get1.status_code == 200
        assert "Email-аккаунт добавлен" in get1.text
        # Second GET — flash should be gone (atomic LRANGE+DEL).
        get2 = await client.get("/accounts")
        assert get2.status_code == 200
        assert "Email-аккаунт добавлен" not in get2.text

    async def test_failed_form_create_renders_inline_flash(
        self, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from backend.app.accounts import service as svc_mod
        from backend.app.exceptions import IMAPLoginFailedError

        async def _bad(**_: Any) -> None:
            raise IMAPLoginFailedError("test login fail")

        monkeypatch.setattr(svc_mod, "imap_test_login", _bad)

        csrf = await _login(client)
        resp = await client.post(
            "/api/mail-accounts",
            data={
                "csrf_token": csrf,
                "email": "bad@example.com",
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
        # On error, current code re-renders the form with a 422 status
        # (IMAPLoginFailedError.status_code = 422).
        assert resp.status_code == 422
        assert "test login fail" in resp.text or "imap_login_failed" in resp.text.lower()
