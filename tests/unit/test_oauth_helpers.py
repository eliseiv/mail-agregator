"""Pure unit tests for OAuth2 Outlook helpers (ADR-0025) — no DB/Redis/network.

Covers:
- J: ``build_xoauth2_string`` SASL format (RFC 7628 / Microsoft).
- B (config part): ``outlook_oauth_enabled`` / endpoint derivation.
- PKCE S256 pair + id_token email decode helpers.
- K: structlog redact-list contains every OAuth secret key.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os

import pytest

from backend.app.accounts.testers import build_xoauth2_string
from backend.app.oauth import service as svc_mod
from shared.config import Settings

pytestmark = pytest.mark.unit

_VALID_KEY = base64.b64encode(b"\x00" * 32).decode()
_REQUIRED = {
    "MAIL_ENCRYPTION_KEY": _VALID_KEY,
}


# ``Settings`` declares ``env_file=".env"`` (shared/config.py) and also reads
# ``os.environ``. Both are developer-machine state: a real ``.env`` (or an
# exported ``OUTLOOK_*``) leaks live credentials into these pure unit tests and
# flips ``outlook_oauth_enabled`` True where the test asserts False. This
# fixture pins the inputs to exactly ``_REQUIRED`` + explicit overrides:
# ``_env_file=None`` disables the dotenv source, and the ``OUTLOOK_*`` keys are
# stripped from the process env.
@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in [k for k in os.environ if k.startswith("OUTLOOK_")]:
        monkeypatch.delenv(key, raising=False)


def _settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **{**_REQUIRED, **overrides})  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# J. build_xoauth2_string
# ---------------------------------------------------------------------------


class TestBuildXoauth2String:
    def test_format_matches_rfc7628(self) -> None:
        b64 = build_xoauth2_string("user@outlook.com", "ATtok123")
        decoded = base64.b64decode(b64).decode("utf-8")
        assert decoded == "user=user@outlook.com\x01auth=Bearer ATtok123\x01\x01"

    def test_output_is_ascii_base64(self) -> None:
        b64 = build_xoauth2_string("u@x.com", "tok")
        # Round-trips cleanly and contains only ASCII base64 chars.
        assert base64.b64encode(base64.b64decode(b64)).decode("ascii") == b64

    def test_control_bytes_present(self) -> None:
        decoded = base64.b64decode(build_xoauth2_string("a@b.co", "T")).decode()
        # Exactly two \x01 separators and a trailing \x01.
        assert decoded.count("\x01") == 3
        assert decoded.endswith("\x01\x01")


# ---------------------------------------------------------------------------
# B (config). Feature-flag derivation + endpoints.
# ---------------------------------------------------------------------------


class TestOutlookOAuthEnabledFlag:
    def test_disabled_when_no_credentials(self) -> None:
        assert _settings().outlook_oauth_enabled is False

    def test_disabled_with_only_client_id(self) -> None:
        assert _settings(OUTLOOK_CLIENT_ID="cid").outlook_oauth_enabled is False

    def test_disabled_with_only_secret(self) -> None:
        assert _settings(OUTLOOK_CLIENT_SECRET="sec").outlook_oauth_enabled is False

    def test_enabled_with_both(self) -> None:
        s = _settings(OUTLOOK_CLIENT_ID="cid", OUTLOOK_CLIENT_SECRET="sec")
        assert s.outlook_oauth_enabled is True

    def test_endpoints_use_tenant(self) -> None:
        s = _settings(OUTLOOK_TENANT="consumers")
        assert s.outlook_authorize_endpoint == (
            "https://login.microsoftonline.com/consumers/oauth2/v2.0/authorize"
        )
        assert s.outlook_token_endpoint == (
            "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
        )


# ---------------------------------------------------------------------------
# PKCE + id_token email decode helpers.
# ---------------------------------------------------------------------------


class TestPkcePair:
    def test_challenge_is_s256_of_verifier(self) -> None:
        verifier, challenge = svc_mod._make_pkce_pair()
        expected = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
            .rstrip(b"=")
            .decode("ascii")
        )
        assert challenge == expected

    def test_verifier_is_url_safe_unpadded(self) -> None:
        verifier, challenge = svc_mod._make_pkce_pair()
        for token in (verifier, challenge):
            assert "=" not in token
            assert "+" not in token and "/" not in token

    def test_pairs_are_random(self) -> None:
        assert svc_mod._make_pkce_pair()[0] != svc_mod._make_pkce_pair()[0]


def _fake_id_token(payload: dict[str, object]) -> str:
    seg = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"header.{seg}.sig"


class TestDecodeEmailFromIdToken:
    def test_reads_email_claim(self) -> None:
        tok = _fake_id_token({"email": "Box@Outlook.com"})
        assert svc_mod._decode_email_from_id_token(tok) == "Box@Outlook.com"

    def test_falls_back_to_preferred_username(self) -> None:
        tok = _fake_id_token({"preferred_username": "alt@outlook.com"})
        assert svc_mod._decode_email_from_id_token(tok) == "alt@outlook.com"

    def test_none_for_missing_token(self) -> None:
        assert svc_mod._decode_email_from_id_token(None) is None

    def test_none_for_malformed_token(self) -> None:
        assert svc_mod._decode_email_from_id_token("not-a-jwt") is None

    def test_none_when_claim_is_not_an_email(self) -> None:
        tok = _fake_id_token({"email": "no-at-sign"})
        assert svc_mod._decode_email_from_id_token(tok) is None


# ---------------------------------------------------------------------------
# K. Redact-list completeness.
# ---------------------------------------------------------------------------


class TestRedactList:
    @pytest.mark.parametrize(
        "key",
        [
            "code",
            "code_verifier",
            "access_token",
            "refresh_token",
            "oauth_access_token",
            "oauth_refresh_token",
            "client_secret",
            "OUTLOOK_CLIENT_SECRET",
            "id_token",
        ],
    )
    def test_secret_key_is_redacted(self, key: str) -> None:
        from shared.logging import REDACT_KEYS, _redact_processor

        assert key in REDACT_KEYS
        out = _redact_processor(None, "info", {key: "super-secret-value"})
        assert out[key] == "[REDACTED]"

    def test_non_secret_key_passes_through(self) -> None:
        from shared.logging import _redact_processor

        out = _redact_processor(None, "info", {"mail_account_id": 7})
        assert out["mail_account_id"] == 7
