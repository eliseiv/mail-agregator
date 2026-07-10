"""Unit tests for the headless-OAuth CRM ingest notification (ADR-0045 §3).

Covers ``backend.app.oauth.crm_ingest.notify_crm_oauth_ingest`` at the only external
boundary (``httpx.AsyncClient``, mocked): the HMAC is computed over the EXACT bytes sent
(``content=raw_body``, never ``json=``), non-ASCII ``display_name`` (Cyrillic) round-trips
byte-for-byte, the connect-only retry budget (retry solely on ``ConnectError`` /
``ConnectTimeout``; any other transport error / non-2xx stops), and best-effort semantics
(disabled config → no request, delivery failure → ``False`` and never raises). Settings are
built hermetically; no process env / network.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

import httpx
import pytest

from backend.app.oauth import crm_ingest as mod
from shared.config import Settings

pytestmark = pytest.mark.unit

_VALID_KEY = "HSoYMcwRZLguwQpz+kHPwifN9LvO/H86royMLyRgclo="
_REQUIRED = {
    "MAIL_ENCRYPTION_KEY": _VALID_KEY,
    "ADMIN_PASSWORD": "x",
    "S3_ACCESS_KEY": "x",
    "S3_SECRET_KEY": "x",
}
_URL = "https://crm.example/api/mail/oauth/ingest"
_SECRET = "shared-oauth-hmac-secret-v1"


def _settings(**overrides: object) -> Settings:
    return Settings(**{**_REQUIRED, **overrides})  # type: ignore[arg-type]


class _Recorder:
    """Captures the POST(s) made by ``notify_crm_oauth_ingest``."""

    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []


def _install_client(
    monkeypatch: pytest.MonkeyPatch,
    rec: _Recorder,
    *,
    status: int = 200,
    raise_exc: Exception | None = None,
) -> None:
    class _FakeResponse:
        status_code = status

        @property
        def text(self) -> str:
            return "ok"

    class _FakeClient:
        def __init__(self, *_a: Any, **_kw: Any) -> None:
            pass

        async def __aenter__(self) -> _FakeClient:
            return self

        async def __aexit__(self, *_a: Any) -> None:
            return None

        async def post(self, url: str, *, content: bytes, headers: dict[str, str]) -> _FakeResponse:
            rec.posts.append({"url": url, "content": content, "headers": headers})
            if raise_exc is not None:
                raise raise_exc
            return _FakeResponse()

    monkeypatch.setattr(mod.httpx, "AsyncClient", _FakeClient)


def _patch_settings(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    monkeypatch.setattr(mod, "get_settings", lambda: settings)


async def _call(**over: Any) -> bool:
    base: dict[str, Any] = {
        "crm_state": "Zm9vLmJhcg",
        "mail_account_id": 7,
        "email": "box@outlook.com",
        "display_name": "Иван Пётр 📧",
        "is_active": True,
    }
    base.update(over)
    return await mod.notify_crm_oauth_ingest(**base)


# --------------------------------------------------------------- disabled config
async def test_disabled_when_url_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _Recorder()
    _install_client(monkeypatch, rec)
    _patch_settings(monkeypatch, _settings(CRM_PUSH_SECRET=_SECRET))  # no URL
    assert await _call() is False
    assert rec.posts == []  # no HTTP attempted


async def test_disabled_when_secret_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _Recorder()
    _install_client(monkeypatch, rec)
    _patch_settings(monkeypatch, _settings(CRM_OAUTH_INGEST_URL=_URL))  # no secret
    assert await _call() is False
    assert rec.posts == []


# ------------------------------------------------------------------- success 2xx
async def test_success_delivers_signed_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _Recorder()
    _install_client(monkeypatch, rec, status=200)
    _patch_settings(monkeypatch, _settings(CRM_OAUTH_INGEST_URL=_URL, CRM_PUSH_SECRET=_SECRET))
    assert await _call() is True
    assert len(rec.posts) == 1
    post = rec.posts[0]
    assert post["url"] == _URL

    # Signature header present + over the EXACT sent bytes.
    ts = int(post["headers"]["X-Mail-Timestamp"])
    sent = post["content"]
    provided = post["headers"]["X-Mail-Signature"]
    assert provided.startswith("sha256=")
    expected = hmac.new(
        _SECRET.encode("utf-8"),
        str(ts).encode("ascii") + b"." + sent,
        hashlib.sha256,
    ).hexdigest()
    assert provided == f"sha256={expected}"


async def test_non_ascii_display_name_serialised_utf8_not_escaped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rec = _Recorder()
    _install_client(monkeypatch, rec, status=200)
    _patch_settings(monkeypatch, _settings(CRM_OAUTH_INGEST_URL=_URL, CRM_PUSH_SECRET=_SECRET))
    await _call(display_name="Иван Пётр 📧")
    sent = rec.posts[0]["content"]
    # Raw UTF-8 (ensure_ascii=False), not \uXXXX escapes.
    assert b"\\u" not in sent
    assert "Иван Пётр 📧" in sent.decode("utf-8")
    # Body is the signed bytes and decodes to the expected fields.
    obj = json.loads(sent)
    assert obj["display_name"] == "Иван Пётр 📧"
    assert obj["mail_account_id"] == 7
    assert obj["crm_state"] == "Zm9vLmJhcg"


async def test_body_has_no_team_id_field(monkeypatch: pytest.MonkeyPatch) -> None:
    """CRM derives team_id from the signed crm_state — the wire body must not carry it."""
    rec = _Recorder()
    _install_client(monkeypatch, rec, status=200)
    _patch_settings(monkeypatch, _settings(CRM_OAUTH_INGEST_URL=_URL, CRM_PUSH_SECRET=_SECRET))
    await _call()
    obj = json.loads(rec.posts[0]["content"])
    assert "team_id" not in obj
    assert set(obj.keys()) == {
        "crm_state",
        "mail_account_id",
        "email",
        "display_name",
        "is_active",
    }


# ---------------------------------------------------------------- non-2xx → False
async def test_non_2xx_returns_false_no_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _Recorder()
    _install_client(monkeypatch, rec, status=401)
    _patch_settings(monkeypatch, _settings(CRM_OAUTH_INGEST_URL=_URL, CRM_PUSH_SECRET=_SECRET))
    assert await _call() is False
    assert len(rec.posts) == 1  # a response was seen → no retry (anti-double-write)


# ---------------------------------------------- connect error → retried, then False
async def test_connect_error_retries_then_false(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _Recorder()
    _install_client(monkeypatch, rec, raise_exc=httpx.ConnectError("no route"))
    _patch_settings(monkeypatch, _settings(CRM_OAUTH_INGEST_URL=_URL, CRM_PUSH_SECRET=_SECRET))
    assert await _call() is False
    # Connect never established → double-write-safe → full retry budget of 3.
    assert len(rec.posts) == mod._CONNECT_RETRY_ATTEMPTS == 3


async def test_connect_timeout_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _Recorder()
    _install_client(monkeypatch, rec, raise_exc=httpx.ConnectTimeout("slow"))
    _patch_settings(monkeypatch, _settings(CRM_OAUTH_INGEST_URL=_URL, CRM_PUSH_SECRET=_SECRET))
    assert await _call() is False
    assert len(rec.posts) == 3


# ------------------------------------- other transport error → NO retry, False
async def test_read_timeout_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """A read timeout may mean the CRM already saw the body → stop (anti-double-write)."""
    rec = _Recorder()
    _install_client(monkeypatch, rec, raise_exc=httpx.ReadTimeout("read"))
    _patch_settings(monkeypatch, _settings(CRM_OAUTH_INGEST_URL=_URL, CRM_PUSH_SECRET=_SECRET))
    assert await _call() is False
    assert len(rec.posts) == 1  # single attempt, not retried


async def test_never_raises_on_transport_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Best-effort: the caller (callback) must never see an exception (§3)."""
    rec = _Recorder()
    _install_client(monkeypatch, rec, raise_exc=httpx.WriteError("boom"))
    _patch_settings(monkeypatch, _settings(CRM_OAUTH_INGEST_URL=_URL, CRM_PUSH_SECRET=_SECRET))
    result = await _call()  # must not raise
    assert result is False


# ------------------------------------------------ config derivation (enabled flag)
class TestCrmOauthIngestEnabledFlag:
    def test_disabled_when_config_empty(self) -> None:
        assert _settings().crm_oauth_ingest_enabled is False

    def test_disabled_with_only_url(self) -> None:
        assert _settings(CRM_OAUTH_INGEST_URL=_URL).crm_oauth_ingest_enabled is False

    def test_disabled_with_only_secret(self) -> None:
        assert _settings(CRM_PUSH_SECRET=_SECRET).crm_oauth_ingest_enabled is False

    def test_enabled_with_both(self) -> None:
        s = _settings(CRM_OAUTH_INGEST_URL=_URL, CRM_PUSH_SECRET=_SECRET)
        assert s.crm_oauth_ingest_enabled is True
