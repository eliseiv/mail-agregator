"""External WRITE-API auth-flow ordering + secret non-disclosure (ADR-0039 / ADR-0040).

Every write endpoint runs the strict sequence ``_authorize_write`` then body:

  1. ``consume(LIMIT_EXTERNAL_WRITE, ip)``      → 429 on exhaustion (FIRST)
  2-4. key extract + feature gate + constant-time compare → 401 (opaque)
  5. write-gate ``EXTERNAL_WRITE_ENABLED``       → 403 (valid key, write off)
  6. body parsed MANUALLY (``_parse_json_body``) → 400 (LAST)

The security-critical invariant (ADR-0039 §Security): with the write-gate OFF a
**valid** key yields ``403`` while an **invalid** key yields ``401`` — the two
are distinguishable ONLY to a holder of the real key, so a prober cannot learn
"the feature exists but is disabled" (config non-disclosure). The API key is
never echoed in a response or an error body.

Source of truth: ``backend/app/external/router.py`` (``_authorize_write`` +
``_parse_json_body``) + ``docs/04-api-contracts.md`` §4f.

Only the HTTP boundary is exercised; the rate-limit runs against real Redis and
the gate/auth against real settings (feature flags flipped via the conftest
``set_external_*`` fixtures — never a mock of our own code).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest

pytestmark = pytest.mark.integration

_TAGS = "/api/external/tags"
_MAILBOX_TEST = "/api/external/mailboxes/test"
# A well-formed create-tag body so a 400 only ever comes from the body step
# being *reached*, never from a malformed payload we sent by accident.
_GOOD_TAG_BODY = {"name": "auth-probe", "color": "#2563eb"}


class TestRateLimitFirst:
    async def test_429_consumed_before_auth(
        self,
        client: httpx.AsyncClient,
        set_external_api_key: Callable[[str], None],
        set_external_write_enabled: Callable[[bool], None],
        set_external_write_rate_limit: Callable[[int], None],
    ) -> None:
        """With the write budget = 1, the FIRST no-key request 401s (rate-limit
        passes, auth fails); the SECOND no-key request 429s — proving the
        ``consume`` runs BEFORE the key check (a failed-auth flood is throttled).
        """
        # Feature enabled so a keyless request would otherwise be a clean 401.
        set_external_api_key("k" * 40)
        set_external_write_enabled(True)
        set_external_write_rate_limit(1)

        r1 = await client.post(_TAGS, json=_GOOD_TAG_BODY)  # no key
        assert r1.status_code == 401, r1.text
        r2 = await client.post(_TAGS, json=_GOOD_TAG_BODY)  # no key, budget spent
        assert r2.status_code == 429, r2.text


class TestAuthBeforeGate:
    async def test_feature_off_any_request_is_401(
        self,
        client: httpx.AsyncClient,
        set_external_api_key: Callable[[str], None],
        set_external_write_enabled: Callable[[bool], None],
    ) -> None:
        """``EXTERNAL_API_KEY`` empty (whole external feature off) → 401 even with
        the write-gate on and a well-formed body. The config is not disclosed."""
        set_external_api_key("")  # feature OFF
        set_external_write_enabled(True)
        resp = await client.post(_TAGS, headers={"X-API-Key": "anything"}, json=_GOOD_TAG_BODY)
        assert resp.status_code == 401, resp.text
        assert resp.json()["error"]["code"] == "not_authenticated"

    async def test_wrong_key_is_401_even_when_write_enabled(
        self,
        client: httpx.AsyncClient,
        set_external_api_key: Callable[[str], None],
        set_external_write_enabled: Callable[[bool], None],
    ) -> None:
        """A WRONG key → 401 before the write-gate is consulted (auth is step 2-4,
        gate is step 5)."""
        set_external_api_key("correct-key-value-00000000000000000000")
        set_external_write_enabled(True)
        resp = await client.post(
            _TAGS, headers={"X-API-Key": "wrong-key-value"}, json=_GOOD_TAG_BODY
        )
        assert resp.status_code == 401, resp.text
        assert resp.json()["error"]["code"] == "not_authenticated"


class TestGateNonDisclosure:
    """The load-bearing ADR-0039 §Security invariant: write-off → valid=403 /
    invalid=401, so a prober without the key cannot tell the feature exists."""

    async def test_valid_key_write_off_is_403(
        self,
        client: httpx.AsyncClient,
        set_external_api_key: Callable[[str], None],
        set_external_write_enabled: Callable[[bool], None],
    ) -> None:
        key = "the-real-key-value-0000000000000000000000"
        set_external_api_key(key)
        set_external_write_enabled(False)  # write-gate OFF
        resp = await client.post(_TAGS, headers={"X-API-Key": key}, json=_GOOD_TAG_BODY)
        assert resp.status_code == 403, resp.text
        assert resp.json()["error"]["code"] == "forbidden"

    async def test_invalid_key_write_off_is_401(
        self,
        client: httpx.AsyncClient,
        set_external_api_key: Callable[[str], None],
        set_external_write_enabled: Callable[[bool], None],
    ) -> None:
        set_external_api_key("the-real-key-value-0000000000000000000000")
        set_external_write_enabled(False)  # write-gate OFF
        resp = await client.post(
            _TAGS, headers={"X-API-Key": "not-the-real-key"}, json=_GOOD_TAG_BODY
        )
        assert resp.status_code == 401, resp.text
        assert resp.json()["error"]["code"] == "not_authenticated"


class TestBodyValidatedLast:
    async def test_malformed_body_reaches_400_only_after_auth_and_gate(
        self, client: httpx.AsyncClient, write_api_on: str
    ) -> None:
        """Auth + gate pass (valid key, write on) → the malformed body finally
        surfaces as 400 validation_error."""
        resp = await client.post(
            _TAGS,
            headers={"X-API-Key": write_api_on, "Content-Type": "application/json"},
            content=b"{ this is not json",
        )
        assert resp.status_code == 400, resp.text
        assert resp.json()["error"]["code"] == "validation_error"

    async def test_malformed_body_does_not_preempt_401(
        self,
        client: httpx.AsyncClient,
        set_external_api_key: Callable[[str], None],
        set_external_write_enabled: Callable[[bool], None],
    ) -> None:
        """A malformed body with a WRONG key → 401 (auth wins over body — the
        body is parsed only at step 6)."""
        set_external_api_key("real-key-000000000000000000000000000000")
        set_external_write_enabled(True)
        resp = await client.post(
            _TAGS,
            headers={"X-API-Key": "wrong", "Content-Type": "application/json"},
            content=b"{ not json",
        )
        assert resp.status_code == 401, resp.text

    async def test_malformed_body_does_not_preempt_403(
        self,
        client: httpx.AsyncClient,
        set_external_api_key: Callable[[str], None],
        set_external_write_enabled: Callable[[bool], None],
    ) -> None:
        key = "real-key-000000000000000000000000000000"
        set_external_api_key(key)
        set_external_write_enabled(False)
        resp = await client.post(
            _TAGS,
            headers={"X-API-Key": key, "Content-Type": "application/json"},
            content=b"{ not json",
        )
        assert resp.status_code == 403, resp.text


class TestSecretNonDisclosure:
    @pytest.mark.parametrize("path", [_TAGS, _MAILBOX_TEST])
    async def test_key_never_echoed_in_response(
        self, client: httpx.AsyncClient, write_api_on: str, path: str
    ) -> None:
        """The API key must never appear in any response body (success or error)."""
        # A deliberately-invalid body so we get an error envelope back to scan.
        resp = await client.post(path, headers={"X-API-Key": write_api_on}, json={"bad": "body"})
        assert write_api_on not in resp.text, "API key leaked into the response body"

    async def test_password_never_echoed_in_test_response(
        self, client: httpx.AsyncClient, write_api_on: str
    ) -> None:
        """A mailbox-test error response must not echo the submitted password."""
        secret = "sup3r-secret-passw0rd-do-not-leak"
        body: dict[str, Any] = {
            "email": "probe@example.com",
            "password": secret,
            "imap_host": "127.0.0.1",
            "imap_port": 1,
            "imap_ssl": True,
            "smtp_host": "127.0.0.1",
            "smtp_port": 1,
            "smtp_ssl": True,
            "smtp_starttls": False,
        }
        resp = await client.post(_MAILBOX_TEST, headers={"X-API-Key": write_api_on}, json=body)
        assert secret not in resp.text, "password leaked into the response body"
