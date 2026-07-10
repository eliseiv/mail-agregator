"""Unit tests for the headless Outlook-OAuth wiring (ADR-0045 §1/§2) — no DB/Redis/network.

Covers the pure / logic-only surfaces of the headless flow:

- ``OAuthState`` transition-safe payload (crm_state vs user_id, mutually exclusive).
- Cross-flow isolation guards: ``exchange_code_headless`` rejects a state WITHOUT
  ``crm_state`` (a session-minted state); ``exchange_code`` rejects a state WITHOUT
  ``user_id`` (a headless-minted state). The adaptation must not let one flow consume the
  other's state. ``_consume_state`` is stubbed so no Redis is touched.
- ``_require_outlook_oauth_enabled`` — 404 (``NotFoundError``) when the Azure creds are unset.
- The callback HTML pages — no-store, self-contained, no request-derived interpolation.
- ``ExternalOAuthAuthorize`` request/response schema bounds (crm_state 1..512).

The full HTTP gate-order (401/403/404/429) + callback code-exchange + one-shot Redis state
are exercised end-to-end in the ``tests/integration`` suite (outside the CI unit lane —
round-6 login TD); this file pins everything reachable without infrastructure.
"""

from __future__ import annotations

import pytest

from backend.app.exceptions import NotFoundError
from backend.app.external.router import (
    _OAUTH_ERR_MESSAGE,
    _OAUTH_ERR_TITLE,
    _OAUTH_OK_MESSAGE,
    _OAUTH_OK_TITLE,
    _oauth_error_page,
    _oauth_success_page,
    _require_outlook_oauth_enabled,
)
from backend.app.external.schemas import (
    ExternalOAuthAuthorizeRequest,
    ExternalOAuthAuthorizeResponse,
)
from backend.app.oauth.schemas import OAuthState
from backend.app.oauth.service import OAuthError, OutlookOAuthService
from shared.config import Settings

pytestmark = pytest.mark.unit

_VALID_KEY = "HSoYMcwRZLguwQpz+kHPwifN9LvO/H86royMLyRgclo="
_REQUIRED = {
    "MAIL_ENCRYPTION_KEY": _VALID_KEY,
    "ADMIN_PASSWORD": "x",
    "S3_ACCESS_KEY": "x",
    "S3_SECRET_KEY": "x",
}


def _settings(**overrides: object) -> Settings:
    return Settings(**{**_REQUIRED, **overrides})  # type: ignore[arg-type]


# ---------------------------------------------------------------- OAuthState
class TestOAuthStatePayload:
    def test_headless_payload_carries_crm_state(self) -> None:
        st = OAuthState(code_verifier="v", crm_state="opaque-token")
        dumped = OAuthState.model_validate_json(st.model_dump_json())
        assert dumped.crm_state == "opaque-token"
        assert dumped.user_id is None
        assert dumped.code_verifier == "v"

    def test_session_payload_carries_user_id(self) -> None:
        st = OAuthState(code_verifier="v", user_id=42)
        dumped = OAuthState.model_validate_json(st.model_dump_json())
        assert dumped.user_id == 42
        assert dumped.crm_state is None

    def test_defaults_both_none(self) -> None:
        st = OAuthState(code_verifier="v")
        assert st.user_id is None and st.crm_state is None


# --------------------------------------- cross-flow isolation (state guards)
def _service(monkeypatch: pytest.MonkeyPatch, state: OAuthState) -> OutlookOAuthService:
    """Build a service whose ``_consume_state`` returns ``state`` (no Redis touched)."""
    svc = OutlookOAuthService(object(), settings=_settings())  # type: ignore[arg-type]

    async def _fake_consume(_state: str) -> OAuthState:
        return state

    monkeypatch.setattr(svc, "_consume_state", _fake_consume)
    return svc


async def test_headless_rejects_session_state_without_crm_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A session-minted state (user_id set, crm_state None) must NOT feed the headless flow."""
    svc = _service(monkeypatch, OAuthState(code_verifier="v", user_id=7))
    with pytest.raises(OAuthError) as exc:
        await svc.exchange_code_headless(code="c", state="s")
    assert exc.value.code == "oauth_state_invalid"


async def test_session_rejects_headless_state_without_user_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A headless-minted state (crm_state set, user_id None) must NOT feed the session flow."""
    svc = _service(monkeypatch, OAuthState(code_verifier="v", crm_state="opaque"))
    with pytest.raises(OAuthError) as exc:
        await svc.exchange_code(code="c", state="s")
    assert exc.value.code == "oauth_state_invalid"


# --------------------------------------------- feature-flag 404 gate (pure)
class TestRequireOutlookOAuthEnabled:
    def test_raises_not_found_when_disabled(self) -> None:
        # Force creds empty — the dev machine's .env may carry real OUTLOOK_* values.
        disabled = _settings(OUTLOOK_CLIENT_ID="", OUTLOOK_CLIENT_SECRET="")
        assert disabled.outlook_oauth_enabled is False
        with pytest.raises(NotFoundError):
            _require_outlook_oauth_enabled(disabled)

    def test_passes_when_enabled(self) -> None:
        s = _settings(OUTLOOK_CLIENT_ID="cid", OUTLOOK_CLIENT_SECRET="sec")
        # Must not raise.
        _require_outlook_oauth_enabled(s)


# --------------------------------------------- callback HTML pages (pure)
class TestOAuthHtmlPages:
    def test_success_page_no_store_and_copy(self) -> None:
        resp = _oauth_success_page()
        assert resp.headers["cache-control"] == "no-store"
        body = resp.body.decode("utf-8")
        assert _OAUTH_OK_TITLE in body
        assert _OAUTH_OK_MESSAGE in body

    def test_error_page_no_store_and_copy(self) -> None:
        resp = _oauth_error_page()
        assert resp.headers["cache-control"] == "no-store"
        body = resp.body.decode("utf-8")
        assert _OAUTH_ERR_TITLE in body
        assert _OAUTH_ERR_MESSAGE in body

    def test_pages_are_self_contained_html(self) -> None:
        for resp in (_oauth_success_page(), _oauth_error_page()):
            body = resp.body.decode("utf-8")
            assert body.startswith("<!doctype html>")
            # Static inline copy only — no external asset / script reference.
            assert "<script" not in body
            assert "http://" not in body and "https://" not in body


# --------------------------------------------- authorize request/response schema
class TestExternalOAuthSchemas:
    def test_request_accepts_valid_crm_state(self) -> None:
        req = ExternalOAuthAuthorizeRequest(crm_state="a" * 100)
        assert req.crm_state == "a" * 100

    def test_request_rejects_empty_crm_state(self) -> None:
        with pytest.raises(ValueError):
            ExternalOAuthAuthorizeRequest(crm_state="")

    def test_request_rejects_over_512_chars(self) -> None:
        with pytest.raises(ValueError):
            ExternalOAuthAuthorizeRequest(crm_state="x" * 513)

    def test_request_accepts_512_boundary(self) -> None:
        req = ExternalOAuthAuthorizeRequest(crm_state="x" * 512)
        assert len(req.crm_state) == 512

    def test_response_shape(self) -> None:
        resp = ExternalOAuthAuthorizeResponse(authorize_url="https://ms/authorize", state="st")
        dumped = resp.model_dump()
        assert set(dumped.keys()) == {"authorize_url", "state"}
        assert dumped["authorize_url"] == "https://ms/authorize"
        assert dumped["state"] == "st"

    def test_request_body_rejects_extra_fields_or_ignores(self) -> None:
        # crm_state is required; a body missing it must fail validation.
        with pytest.raises(ValueError):
            ExternalOAuthAuthorizeRequest.model_validate({})  # type: ignore[arg-type]
