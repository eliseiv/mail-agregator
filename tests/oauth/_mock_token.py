"""Shared helpers: a mock Microsoft token endpoint via httpx.MockTransport.

No real Azure App / network — the OAuth services accept an injected
``http_client`` (ADR-0025 Q-OAUTH-3 / TD-031) and we feed them a client backed
by a :class:`httpx.MockTransport` whose handler we control per-test.

SINGLE-STEP flow (working Sprint-B config, P1/P2 reverted): ``exchange_code``
issues exactly ONE ``code -> token`` request with the direct
``https://outlook.office.com/…`` resource scopes; refresh reuses the same
scopes. There is no two-step audience upgrade.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from urllib.parse import parse_qsl

import httpx


def make_id_token(email: str) -> str:
    """Build an unsigned (header.payload.sig) JWT carrying an ``email`` claim.

    The service deliberately does NOT verify the signature (trusted transport),
    so a syntactically valid 3-segment token with the claim suffices.
    """
    payload = base64.urlsafe_b64encode(json.dumps({"email": email}).encode()).rstrip(b"=").decode()
    return f"e30.{payload}.sig"


def token_success_body(
    *,
    access_token: str = "ATtok-AAA",
    refresh_token: str | None = "RTtok-AAA",
    expires_in: int = 3600,
    scope: str = (
        "https://outlook.office.com/IMAP.AccessAsUser.All "
        "https://outlook.office.com/SMTP.Send offline_access"
    ),
    email: str | None = "user@outlook.com",
) -> dict[str, object]:
    body: dict[str, object] = {
        "access_token": access_token,
        "expires_in": expires_in,
        "scope": scope,
        "token_type": "Bearer",
    }
    if refresh_token is not None:
        body["refresh_token"] = refresh_token
    if email is not None:
        body["id_token"] = make_id_token(email)
    return body


def _parse_form(request: httpx.Request) -> dict[str, str]:
    """URL-decode the posted ``application/x-www-form-urlencoded`` body.

    Tests assert the request ``scope``, which contains URL-reserved characters
    (``:`` ``/`` in ``https://outlook.office.com/…``), so we must percent-decode
    — a plain ``split("&")`` leaves them encoded.
    """
    body = request.content.decode()
    return dict(parse_qsl(body, keep_blank_values=True))


class TokenEndpoint:
    """Counts calls and replays a queue of canned responses.

    Each entry in ``responses`` is either an ``httpx.Response`` or a callable
    ``(request) -> httpx.Response``. When the queue is exhausted the last entry
    is reused (so a single success response serves repeated refreshes).

    ``requests`` records the decoded form body of EVERY call in order, so tests
    can assert ``grant_type`` / ``scope``. ``last_request_data`` is kept as an
    alias of the most recent entry for single-call tests.
    """

    def __init__(
        self,
        responses: list[httpx.Response | Callable[[httpx.Request], httpx.Response]] | None = None,
    ) -> None:
        self.calls = 0
        self.requests: list[dict[str, str]] = []
        self.last_request_data: dict[str, str] = {}
        self._responses = responses or [httpx.Response(200, json=token_success_body())]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        # Capture the posted form so tests can assert grant_type / scope.
        data = _parse_form(request)
        self.requests.append(data)
        self.last_request_data = data
        idx = min(self.calls - 1, len(self._responses) - 1)
        entry = self._responses[idx]
        return entry(request) if callable(entry) else entry

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self))
