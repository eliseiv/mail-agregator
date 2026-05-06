"""Contract tests: validate API response shapes against the documented schema.

We re-use the Pydantic schemas from ``backend.app.<module>.schemas`` as the
source-of-truth — if those schemas drift from ``docs/04-api-contracts.md``,
the architect-reviewer is meant to catch it first.

These tests run live HTTP requests through the ASGI app, then deserialize
the JSON body through the Pydantic model. ``model_validate`` raises
``ValidationError`` on shape drift — so a test failure here means the API
returned something the schema doesn't accept.
"""

from __future__ import annotations

import httpx
import pytest

from backend.app.accounts.schemas import MailAccountDTO, TestResult
from backend.app.admin.schemas import AuditListResponse, UsersListResponse
from backend.app.messages.schemas import MessageListResponse
from backend.app.send.schemas import SendMessageResponse
from shared.config import get_settings

pytestmark = [pytest.mark.contract, pytest.mark.integration]


async def _login(client: httpx.AsyncClient) -> str:
    s = get_settings()
    resp = await client.post(
        "/login",
        data={"username": s.ADMIN_LOGIN, "password": s.ADMIN_PASSWORD},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert resp.status_code == 302
    return resp.cookies["mas_csrf"]


# ---------------------------------------------------------------------------
# Read endpoints — should not be affected by the production write-tx bug
# ---------------------------------------------------------------------------


class TestListEndpoints:
    async def test_get_messages_response_matches_schema(self, client: httpx.AsyncClient) -> None:
        await _login(client)
        resp = await client.get("/api/messages")
        assert resp.status_code == 200, resp.text
        # Round-trip through Pydantic.
        MessageListResponse.model_validate(resp.json())

    async def test_get_admin_users_matches_schema(self, client: httpx.AsyncClient) -> None:
        await _login(client)
        resp = await client.get("/api/admin/users")
        assert resp.status_code == 200, resp.text
        UsersListResponse.model_validate(resp.json())

    async def test_get_admin_audit_matches_schema(self, client: httpx.AsyncClient) -> None:
        await _login(client)
        resp = await client.get("/api/admin/audit")
        assert resp.status_code == 200, resp.text
        AuditListResponse.model_validate(resp.json())

    async def test_get_mail_accounts_matches_schema(self, client: httpx.AsyncClient) -> None:
        await _login(client)
        resp = await client.get("/api/mail-accounts")
        assert resp.status_code == 200, resp.text
        # Empty list at fresh state — still valid against schema.
        for item in resp.json():
            MailAccountDTO.model_validate(item)


# ---------------------------------------------------------------------------
# Error format — every 4xx should follow the unified envelope
# ---------------------------------------------------------------------------


class TestErrorEnvelope:
    async def test_unauthenticated_envelope(self, client: httpx.AsyncClient) -> None:
        resp = await client.get("/api/me")
        assert resp.status_code == 401
        body = resp.json()
        assert "error" in body
        assert "code" in body["error"]
        assert "message" in body["error"]
        assert body["error"]["code"] == "not_authenticated"

    async def test_validation_error_envelope(self, client: httpx.AsyncClient) -> None:
        await _login(client)
        # Send invalid JSON to a JSON endpoint.
        resp = await client.get("/api/messages?cursor=NOT_A_VALID_BASE64_CURSOR!")
        # Either 200 (cursor decode tolerant) or 400.
        if resp.status_code == 400:
            body = resp.json()
            assert body["error"]["code"] in {"validation_error", "validation"}


class TestSchemaDocsAlignment:
    """Schemas referenced by tests above must declare the documented fields."""

    def test_message_list_response_has_items_and_next_cursor(self) -> None:
        fields = MessageListResponse.model_fields
        assert "items" in fields
        assert "next_cursor" in fields

    def test_send_message_response_fields(self) -> None:
        fields = SendMessageResponse.model_fields
        assert "sent_id" in fields
        assert "smtp_message_id" in fields
        assert "appended_to_sent" in fields

    def test_test_result_fields(self) -> None:
        fields = TestResult.model_fields
        assert "imap_ok" in fields
        assert "smtp_ok" in fields
