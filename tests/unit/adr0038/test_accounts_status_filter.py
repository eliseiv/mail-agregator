"""ADR-0038 Part 1 — ``/accounts?status=all|active|inactive`` filter.

Covered:
- HTTP: an invalid ``status`` value → 4xx validation error (the app normalises
  RequestValidationError to 400); the default (absent) is ``all`` → 200;
- service: after visibility resolution the returned set is filtered correctly for
  both a super_admin scope and a group_leader scope.

Source of truth: ``backend/app/accounts/router.py`` (accounts_page) +
``backend/app/accounts/service.py`` (`list_for_scope`) + plan Part 1.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from backend.app.accounts.schemas import MailAccountCreateRequest
from backend.app.accounts.service import MailAccountService
from backend.app.deps import VisibilityScope
from shared.models import ROLE_GROUP_LEADER, ROLE_SUPER_ADMIN

pytestmark = pytest.mark.integration


async def _login_admin(client: httpx.AsyncClient) -> str:
    from tests.integration.conftest import login_as_admin

    return await login_as_admin(client)


# ---------------------------------------------------------------------------
# HTTP-level: query validation + default
# ---------------------------------------------------------------------------


class TestStatusQueryValidation:
    async def test_invalid_status_rejected(self, client: httpx.AsyncClient) -> None:
        # The app normalises FastAPI's RequestValidationError to a 400 with a
        # ``validation_error`` envelope app-wide (ADR-0014 ``_validation_handler``),
        # rather than the framework default 422. Either is a valid "rejected".
        await _login_admin(client)
        r = await client.get("/accounts?status=bogus")
        assert r.status_code in (400, 422), r.text
        assert r.json()["error"]["code"] == "validation_error"

    async def test_default_status_is_all_ok(self, client: httpx.AsyncClient) -> None:
        await _login_admin(client)
        r = await client.get("/accounts")
        assert r.status_code == 200, r.text

    @pytest.mark.parametrize("value", ["all", "active", "inactive"])
    async def test_valid_status_values_ok(self, client: httpx.AsyncClient, value: str) -> None:
        await _login_admin(client)
        r = await client.get(f"/accounts?status={value}")
        assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# Service-level: correct filtered set after visibility
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _mock_test_login(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub IMAP/SMTP test-login so account creation never touches the network."""
    from backend.app.accounts import service as svc_mod

    async def _ok(**_: Any) -> None:
        return None

    monkeypatch.setattr(svc_mod, "imap_test_login", _ok)
    monkeypatch.setattr(svc_mod, "smtp_test_login", _ok)


def _payload(email: str) -> MailAccountCreateRequest:
    return MailAccountCreateRequest(
        email=email,
        password="secret-imap-pwd",
        imap_host="imap.example.com",
        imap_port=993,
        imap_ssl=True,
        smtp_host="smtp.example.com",
        smtp_port=465,
        smtp_ssl=True,
        smtp_starttls=False,
    )


async def _seed_leader_with_two_accounts(
    client: httpx.AsyncClient, csrf: str, db_engine: AsyncEngine
) -> tuple[int, int, int, int]:
    """Create a leader (auto-group) + one active + one inactive account owned by
    them. Returns ``(leader_id, group_id, active_acc_id, inactive_acc_id)``."""
    resp = await client.post(
        "/api/admin/users",
        json={"username": "lead_accts", "role": "group_leader"},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 201, resp.text
    leader = resp.json()
    leader_id, group_id = leader["id"], leader["group_id"]
    owner_scope = VisibilityScope(
        user_id=leader_id,
        role=ROLE_GROUP_LEADER,
        group_id=group_id,
        group_ids=frozenset({group_id}),
    )

    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with factory() as ses:
        svc = MailAccountService(ses)
        async with ses.begin():
            active = await svc.create(scope=owner_scope, payload=_payload("active@x.com"))
            inactive = await svc.create(scope=owner_scope, payload=_payload("inactive@x.com"))
        # Flip one account to inactive directly (server-default is active).
        async with ses.begin():
            await ses.execute(
                text("UPDATE mail_accounts SET is_active = false WHERE id = :id"),
                {"id": inactive.id},
            )
    return leader_id, group_id, active.id, inactive.id


class TestStatusServiceFilter:
    async def test_super_admin_scope_filters_by_status(
        self, client: httpx.AsyncClient, db_engine: AsyncEngine
    ) -> None:
        csrf = await _login_admin(client)
        _lid, _gid, active_id, inactive_id = await _seed_leader_with_two_accounts(
            client, csrf, db_engine
        )
        scope = VisibilityScope(
            user_id=1, role=ROLE_SUPER_ADMIN, group_id=None, group_ids=frozenset()
        )
        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as ses:
            svc = MailAccountService(ses)
            all_ids = {d.id for d in await svc.list_for_scope(scope, status="all")}
            active_ids = {d.id for d in await svc.list_for_scope(scope, status="active")}
            inactive_ids = {d.id for d in await svc.list_for_scope(scope, status="inactive")}

        assert {active_id, inactive_id} <= all_ids
        assert active_id in active_ids and inactive_id not in active_ids
        assert inactive_id in inactive_ids and active_id not in inactive_ids

    async def test_leader_scope_filters_by_status(
        self, client: httpx.AsyncClient, db_engine: AsyncEngine
    ) -> None:
        csrf = await _login_admin(client)
        leader_id, group_id, active_id, inactive_id = await _seed_leader_with_two_accounts(
            client, csrf, db_engine
        )
        scope = VisibilityScope(
            user_id=leader_id,
            role=ROLE_GROUP_LEADER,
            group_id=group_id,
            group_ids=frozenset({group_id}),
        )
        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as ses:
            svc = MailAccountService(ses)
            all_ids = {d.id for d in await svc.list_for_scope(scope, status="all")}
            active_ids = {d.id for d in await svc.list_for_scope(scope, status="active")}
            inactive_ids = {d.id for d in await svc.list_for_scope(scope, status="inactive")}

        assert all_ids == {active_id, inactive_id}
        assert active_ids == {active_id}
        assert inactive_ids == {inactive_id}
