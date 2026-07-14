"""External WRITE-API — mailbox CRUD (ADR-0039 §2, ``docs/04-api-contracts.md`` §4f).

Covers the mailbox write surface owned by the ``crm-service`` technical user:

- ``POST /mailboxes``        create → owner == ``crm-service``; 409 on dup email;
- ``PATCH /mailboxes/{id}``  ``is_active`` toggle (activate resets the failure
                             counter + alert stamp — ADR-0033).
- ``DELETE /mailboxes/{id}`` 204 + row gone.
- ``POST /mailboxes/{id}/sync`` 202 + Redis ``force_sync:{id}`` marker set.
- ``POST /mailboxes/test``   422 (never 502) on a connection failure; 400 on the
                             ssl/starttls mutual-exclusion.

The IMAP/SMTP probe (external boundary) is stubbed via ``patch_mail_testers`` for
the create/persist paths; the ``test``-endpoint failure cases use a real
fast-failing socket (``127.0.0.1:1``) to prove the 422-not-502 mapping.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from shared.models import MailAccount, User

pytestmark = pytest.mark.integration

_MB = "/api/external/mailboxes"


def _create_body(email: str, **over: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "email": email,
        "password": "app-password-123",
        "imap_host": "imap.example.com",
        "imap_port": 993,
        "imap_ssl": True,
        "smtp_host": "smtp.example.com",
        "smtp_port": 465,
        "smtp_ssl": True,
        "smtp_starttls": False,
    }
    body.update(over)
    return body


async def _owner_id(db_engine: AsyncEngine, account_id: int) -> int:
    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with factory() as ses:
        acc = await ses.get(MailAccount, account_id)
        assert acc is not None
        return int(acc.user_id)


class TestCreate:
    async def test_created_mailbox_is_owned_by_crm_service(
        self,
        client: httpx.AsyncClient,
        write_api_on: str,
        patch_mail_testers: None,
        crm_service_user: User,
        db_engine: AsyncEngine,
    ) -> None:
        resp = await client.post(
            _MB, headers={"X-API-Key": write_api_on}, json=_create_body("new@example.com")
        )
        assert resp.status_code == 201, resp.text
        dto = resp.json()
        # The wire DTO exposes NO owner/user_id — verify ownership in the DB.
        assert "user_id" not in dto and "encrypted_password" not in dto
        assert await _owner_id(db_engine, dto["id"]) == crm_service_user.id
        # Sync-status triplet is present (ADR-0039 §4).
        assert dto["consecutive_failures"] == 0
        assert dto["last_synced_at"] is None
        assert dto["is_active"] is True

    async def test_duplicate_email_is_409(
        self,
        client: httpx.AsyncClient,
        write_api_on: str,
        patch_mail_testers: None,
    ) -> None:
        r1 = await client.post(
            _MB, headers={"X-API-Key": write_api_on}, json=_create_body("dup@example.com")
        )
        assert r1.status_code == 201, r1.text
        r2 = await client.post(
            _MB, headers={"X-API-Key": write_api_on}, json=_create_body("dup@example.com")
        )
        assert r2.status_code == 409, r2.text
        err = r2.json()["error"]
        assert err["code"] == "conflict"
        assert err.get("field") == "email"


class TestPatchIsActive:
    async def test_activate_resets_failure_counter_and_alert_stamp(
        self,
        client: httpx.AsyncClient,
        write_api_on: str,
        owner: User,
        make_mail_account: Callable[..., Any],
        db_engine: AsyncEngine,
    ) -> None:
        """A disabled mailbox with a failure history: ``PATCH is_active=true``
        re-enables it AND clears ``consecutive_failures`` / ``last_sync_error`` /
        the ADR-0033 alert idempotency stamp."""
        acc = await make_mail_account(owner.id, "toggle@example.com")
        # Simulate a worker auto-disable with an alert already sent.
        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as ses, ses.begin():
            await ses.execute(
                text(
                    "UPDATE mail_accounts SET is_active=false, consecutive_failures=5, "
                    "last_sync_error='imap login failed', disabled_alert_sent_at=now() "
                    "WHERE id=:id"
                ),
                {"id": acc.id},
            )

        resp = await client.patch(
            f"{_MB}/{acc.id}", headers={"X-API-Key": write_api_on}, json={"is_active": True}
        )
        assert resp.status_code == 200, resp.text
        dto = resp.json()
        assert dto["is_active"] is True
        assert dto["consecutive_failures"] == 0
        assert dto["last_sync_error"] is None
        # The alert stamp is cleared in the DB (not on the wire DTO).
        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as ses:
            row = (
                await ses.execute(
                    text("SELECT disabled_alert_sent_at FROM mail_accounts WHERE id=:id"),
                    {"id": acc.id},
                )
            ).scalar_one()
        assert row is None, "activate must clear disabled_alert_sent_at (ADR-0033)"

    async def test_deactivate_only_sets_flag(
        self,
        client: httpx.AsyncClient,
        write_api_on: str,
        owner: User,
        make_mail_account: Callable[..., Any],
    ) -> None:
        acc = await make_mail_account(owner.id, "deact@example.com")
        resp = await client.patch(
            f"{_MB}/{acc.id}", headers={"X-API-Key": write_api_on}, json={"is_active": False}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["is_active"] is False


class TestPatchUnknown:
    async def test_patch_unknown_mailbox_is_404(
        self, client: httpx.AsyncClient, write_api_on: str
    ) -> None:
        resp = await client.patch(
            f"{_MB}/987654", headers={"X-API-Key": write_api_on}, json={"is_active": True}
        )
        assert resp.status_code == 404, resp.text


class TestDelete:
    async def test_delete_removes_row(
        self,
        client: httpx.AsyncClient,
        write_api_on: str,
        owner: User,
        make_mail_account: Callable[..., Any],
        db_engine: AsyncEngine,
    ) -> None:
        acc = await make_mail_account(owner.id, "del@example.com")
        resp = await client.delete(f"{_MB}/{acc.id}", headers={"X-API-Key": write_api_on})
        assert resp.status_code == 204, resp.text
        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as ses:
            assert await ses.get(MailAccount, acc.id) is None

    async def test_delete_unknown_is_404(
        self, client: httpx.AsyncClient, write_api_on: str
    ) -> None:
        resp = await client.delete(f"{_MB}/987654", headers={"X-API-Key": write_api_on})
        assert resp.status_code == 404, resp.text


class TestSync:
    async def test_sync_returns_202_and_sets_redis_marker(
        self,
        client: httpx.AsyncClient,
        write_api_on: str,
        owner: User,
        make_mail_account: Callable[..., Any],
    ) -> None:
        acc = await make_mail_account(owner.id, "sync@example.com")
        resp = await client.post(f"{_MB}/{acc.id}/sync", headers={"X-API-Key": write_api_on})
        assert resp.status_code == 202, resp.text
        assert resp.json()["queued"] is True
        from shared.redis_client import get_redis

        marker = await get_redis().get(f"force_sync:{acc.id}")
        assert marker is not None, "force_sync marker must be set for the worker to pick up"

    async def test_sync_unknown_mailbox_is_404(
        self, client: httpx.AsyncClient, write_api_on: str
    ) -> None:
        resp = await client.post(f"{_MB}/987654/sync", headers={"X-API-Key": write_api_on})
        assert resp.status_code == 404, resp.text


class TestTestEndpoint:
    async def test_connection_failure_is_422_never_502(
        self, client: httpx.AsyncClient, write_api_on: str
    ) -> None:
        """A dead IMAP/SMTP target → 422 (imap/smtp_login_failed), NEVER 502.
        502 ``smtp_failed`` is reserved for the send/reply path (ADR-0039 §2)."""
        body: dict[str, Any] = {
            "email": "probe@example.com",
            "password": "x",
            "imap_host": "127.0.0.1",
            "imap_port": 1,  # nothing listening → connection refused, fast
            "imap_ssl": True,
            "smtp_host": "127.0.0.1",
            "smtp_port": 1,
            "smtp_ssl": True,
            "smtp_starttls": False,
        }
        resp = await client.post(f"{_MB}/test", headers={"X-API-Key": write_api_on}, json=body)
        assert resp.status_code == 422, resp.text
        assert resp.status_code != 502
        assert resp.json()["error"]["code"] in {"imap_login_failed", "smtp_login_failed"}

    async def test_ssl_and_starttls_mutually_exclusive_is_400(
        self, client: httpx.AsyncClient, write_api_on: str
    ) -> None:
        body: dict[str, Any] = {
            "email": "probe@example.com",
            "password": "x",
            "imap_host": "imap.example.com",
            "imap_port": 993,
            "imap_ssl": True,
            "smtp_host": "smtp.example.com",
            "smtp_port": 587,
            "smtp_ssl": True,
            "smtp_starttls": True,  # both set → schema rejects at parse (400)
        }
        resp = await client.post(f"{_MB}/test", headers={"X-API-Key": write_api_on}, json=body)
        assert resp.status_code == 400, resp.text
        assert resp.json()["error"]["code"] == "validation_error"
