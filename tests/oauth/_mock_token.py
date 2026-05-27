"""Shared helpers: a mock Microsoft token endpoint via httpx.MockTransport.

No real Azure App / network — the OAuth services accept an injected
``http_client`` (ADR-0025 Q-OAUTH-3 / TD-031) and we feed them a client backed
by a :class:`httpx.MockTransport` whose handler we control per-test.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable

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
    scope: str = "https://outlook.office365.com/IMAP.AccessAsUser.All offline_access",
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


class TokenEndpoint:
    """Counts calls and replays a queue of canned responses.

    Each entry in ``responses`` is either an ``httpx.Response`` or a callable
    ``(request) -> httpx.Response``. When the queue is exhausted the last entry
    is reused (so a single success response serves repeated refreshes).
    """

    def __init__(
        self,
        responses: list[httpx.Response | Callable[[httpx.Request], httpx.Response]] | None = None,
    ) -> None:
        self.calls = 0
        self.last_request_data: dict[str, str] = {}
        self._responses = responses or [httpx.Response(200, json=token_success_body())]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        # Capture the posted form so tests can assert grant_type / params.
        body = request.content.decode()
        self.last_request_data = dict(pair.split("=", 1) for pair in body.split("&") if "=" in pair)
        idx = min(self.calls - 1, len(self._responses) - 1)
        entry = self._responses[idx]
        return entry(request) if callable(entry) else entry

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self))
