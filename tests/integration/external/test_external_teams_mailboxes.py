"""Integration tests for the external teams / mailboxes endpoints (ADR-0037).

``GET /api/external/teams`` — flat list of all system teams (``groups``) for the
internal CRM (``{"teams": [{"id", "name"}]}``). ``GET /api/external/mailboxes``
— canonical-deduped mailboxes with status (``{"mailboxes": [{"id", "email",
"display_name", "group_id", "is_active"}]}``). Both reuse the ADR-0029 §4 auth
flow (rate-limit FIRST → ``X-API-Key``/``Bearer`` → feature gate →
constant-time compare), are CSRF-exempt and super_admin-visible.

Source of truth: ``docs/adr/ADR-0037-external-teams-mailboxes-message-filters.md``
+ ``docs/04-api-contracts.md`` §4d-teams / §4d-mailboxes +
``backend/app/external/{router,service,schemas}.py``.

Only the HTTP boundary is exercised through the network — DB state is seeded
directly against real Postgres (never a mock of our own code) so the
``list_all_groups`` / ``list_canonical_account_ids`` / ``list_by_ids`` paths run
against actual SQL.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from shared.models import MailAccount
from tests.integration.external.conftest import TEST_API_KEY

pytestmark = pytest.mark.integration

_TEAMS_URL = "/api/external/teams"
_MAILBOXES_URL = "/api/external/mailboxes"


async def _set_inactive(db_engine: AsyncEngine, account_id: int) -> None:
    """Flip ``mail_accounts.is_active`` to ``False`` for one account.

    The worker auto-disables a failing mailbox (ADR-0033); the fixture builder
    only ever creates active mailboxes, so we mutate directly to exercise the
    ``is_active=false`` projection path.
    """
    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with factory() as ses, ses.begin():
        await ses.execute(
            update(MailAccount).where(MailAccount.id == account_id).values(is_active=False)
        )


# ===========================================================================
# GET /api/external/teams — auth (ADR-0037 §1, ADR-0029 §4)
# ===========================================================================


class TestTeamsAuth:
    async def test_valid_key_returns_200(self, client: httpx.AsyncClient, api_key_on: str) -> None:
        resp = await client.get(_TEAMS_URL, headers={"X-API-Key": api_key_on})
        assert resp.status_code == 200, resp.text
        assert set(resp.json().keys()) == {"teams"}

    async def test_valid_bearer_returns_200(
        self, client: httpx.AsyncClient, api_key_on: str
    ) -> None:
        resp = await client.get(_TEAMS_URL, headers={"Authorization": f"Bearer {api_key_on}"})
        assert resp.status_code == 200, resp.text

    async def test_no_key_returns_401(self, client: httpx.AsyncClient, api_key_on: str) -> None:
        resp = await client.get(_TEAMS_URL)
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "not_authenticated"

    async def test_wrong_key_returns_401(self, client: httpx.AsyncClient, api_key_on: str) -> None:
        resp = await client.get(_TEAMS_URL, headers={"X-API-Key": "wrong-key"})
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "not_authenticated"

    async def test_feature_off_returns_401(
        self, client: httpx.AsyncClient, set_external_api_key: Callable[[str], None]
    ) -> None:
        # EXTERNAL_API_KEY="" => feature off; even the real test key must 401
        # (opaque — indistinguishable from a wrong key, config never disclosed).
        set_external_api_key("")
        for headers in (
            {},
            {"X-API-Key": TEST_API_KEY},
            {"Authorization": f"Bearer {TEST_API_KEY}"},
        ):
            resp = await client.get(_TEAMS_URL, headers=headers)
            assert resp.status_code == 401, f"{headers} -> {resp.status_code}"
            assert resp.json()["error"]["code"] == "not_authenticated"


class TestTeamsRateLimit:
    async def test_sixth_request_returns_429_with_retry_after(
        self,
        client: httpx.AsyncClient,
        api_key_on: str,
        set_external_rate_limit: Callable[[int], None],
    ) -> None:
        """With the shared ``LIMIT_EXTERNAL_API`` cap lowered to 5, the 6th GET
        from one IP flips to 429 + a positive ``Retry-After``.

        The valid key is sent so the ONLY possible rejection is the rate limit
        (auth would 200) — proving the shared read-budget guards ``/teams`` too.
        """
        set_external_rate_limit(5)
        ip = "203.0.113.31"  # TEST-NET-3 — private to this test
        headers = {"X-API-Key": api_key_on, "X-Forwarded-For": ip}
        for i in range(5):
            r = await client.get(_TEAMS_URL, headers=headers)
            assert r.status_code == 200, f"req {i + 1}/5 -> {r.status_code}: {r.text}"
        sixth = await client.get(_TEAMS_URL, headers=headers)
        assert sixth.status_code == 429, sixth.text
        assert sixth.json()["error"]["code"] == "rate_limited"
        assert 0 < int(sixth.headers["retry-after"]) <= 60


class TestTeamsRedaction:
    async def test_api_key_not_logged(
        self,
        client: httpx.AsyncClient,
        api_key_on: str,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Neither the configured key nor a provided wrong secret appears in
        logs (redact-list: ``EXTERNAL_API_KEY``/``X-API-Key``/``Authorization``)."""
        ok = await client.get(_TEAMS_URL, headers={"X-API-Key": api_key_on})
        assert ok.status_code == 200
        distinctive_wrong = "TEAMS_SECRET_SHOULD_NOT_LEAK_98765"
        bad = await client.get(_TEAMS_URL, headers={"Authorization": f"Bearer {distinctive_wrong}"})
        assert bad.status_code == 401

        out = capsys.readouterr().out
        assert api_key_on not in out, "configured EXTERNAL_API_KEY leaked into logs"
        assert distinctive_wrong not in out, "provided key leaked into logs"


# ===========================================================================
# GET /api/external/teams — content (ADR-0037 §1)
# ===========================================================================


class TestTeamsContent:
    async def test_empty_system_returns_empty_list(
        self, client: httpx.AsyncClient, api_key_on: str
    ) -> None:
        """No groups (the seeded super_admin has ``group_id=NULL``) → ``{teams: []}``."""
        resp = await client.get(_TEAMS_URL, headers={"X-API-Key": api_key_on})
        assert resp.status_code == 200
        assert resp.json() == {"teams": []}

    async def test_lists_all_teams_id_name_only_ordered_by_id(
        self,
        client: httpx.AsyncClient,
        api_key_on: str,
        make_secondary_team_mailbox: Callable[..., Any],
    ) -> None:
        """All system teams are returned (super_admin-visibility), each with ONLY
        ``id``/``name``, ordered by ``id`` (``list_all_groups`` — ADR-0037 §1)."""
        acc_a = await make_secondary_team_mailbox(
            username="team_alpha_u", group_name="Alpha", email="alpha@example.com"
        )
        acc_b = await make_secondary_team_mailbox(
            username="team_beta_u", group_name="Beta", email="beta@example.com"
        )
        assert acc_a.group_id < acc_b.group_id  # creation order == id order

        resp = await client.get(_TEAMS_URL, headers={"X-API-Key": api_key_on})
        assert resp.status_code == 200
        teams = resp.json()["teams"]
        assert teams == [
            {"id": acc_a.group_id, "name": "Alpha"},
            {"id": acc_b.group_id, "name": "Beta"},
        ]
        # Minimal projection: NO leader_user_id / created_at / members_count.
        for t in teams:
            assert set(t.keys()) == {"id", "name"}


# ===========================================================================
# GET /api/external/mailboxes — auth (shares the ADR-0029 §4 flow)
# ===========================================================================


class TestMailboxesAuth:
    async def test_valid_key_returns_200(self, client: httpx.AsyncClient, api_key_on: str) -> None:
        resp = await client.get(_MAILBOXES_URL, headers={"X-API-Key": api_key_on})
        assert resp.status_code == 200, resp.text
        assert set(resp.json().keys()) == {"mailboxes"}

    async def test_no_key_returns_401(self, client: httpx.AsyncClient, api_key_on: str) -> None:
        resp = await client.get(_MAILBOXES_URL)
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "not_authenticated"

    async def test_wrong_key_returns_401(self, client: httpx.AsyncClient, api_key_on: str) -> None:
        resp = await client.get(_MAILBOXES_URL, headers={"X-API-Key": "wrong-key"})
        assert resp.status_code == 401

    async def test_feature_off_returns_401(
        self, client: httpx.AsyncClient, set_external_api_key: Callable[[str], None]
    ) -> None:
        set_external_api_key("")
        resp = await client.get(_MAILBOXES_URL, headers={"X-API-Key": TEST_API_KEY})
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "not_authenticated"

    async def test_sixth_request_returns_429(
        self,
        client: httpx.AsyncClient,
        api_key_on: str,
        set_external_rate_limit: Callable[[int], None],
    ) -> None:
        set_external_rate_limit(5)
        ip = "203.0.113.32"
        headers = {"X-API-Key": api_key_on, "X-Forwarded-For": ip}
        for _ in range(5):
            assert (await client.get(_MAILBOXES_URL, headers=headers)).status_code == 200
        sixth = await client.get(_MAILBOXES_URL, headers=headers)
        assert sixth.status_code == 429
        assert sixth.json()["error"]["code"] == "rate_limited"


# ===========================================================================
# GET /api/external/mailboxes — content (ADR-0037 §2)
# ===========================================================================


class TestMailboxesContent:
    async def _all(self, client: httpx.AsyncClient, key: str) -> list[dict[str, Any]]:
        resp = await client.get(_MAILBOXES_URL, headers={"X-API-Key": key})
        assert resp.status_code == 200, resp.text
        mailboxes: list[dict[str, Any]] = resp.json()["mailboxes"]
        return mailboxes

    async def _one(self, client: httpx.AsyncClient, key: str, account_id: int) -> dict[str, Any]:
        for mb in await self._all(client, key):
            if mb["id"] == account_id:
                return mb
        raise AssertionError(f"mailbox {account_id} not in response")

    async def test_no_mailboxes_returns_empty_list(
        self, client: httpx.AsyncClient, api_key_on: str
    ) -> None:
        resp = await client.get(_MAILBOXES_URL, headers={"X-API-Key": api_key_on})
        assert resp.status_code == 200
        assert resp.json() == {"mailboxes": []}

    async def test_fields_shape_id_email_display_name_group_id_is_active(
        self,
        client: httpx.AsyncClient,
        api_key_on: str,
        make_secondary_team_mailbox: Callable[..., Any],
    ) -> None:
        """A mailbox exposes EXACTLY {id, email, display_name, group_id,
        is_active} with the DB values — and no secret/owner columns."""
        acc = await make_secondary_team_mailbox(
            username="mb_owner",
            group_name="Sales",
            email="Sales.Box@Example.com",
            display_name="Sales Box",
        )
        mb = await self._one(client, api_key_on, acc.id)
        assert mb == {
            "id": acc.id,
            "email": "Sales.Box@Example.com",
            "display_name": "Sales Box",
            "group_id": acc.group_id,
            "is_active": True,
        }
        forbidden = {
            "encrypted_password",
            "smtp_encrypted_password",
            "oauth_refresh_token_encrypted",
            "oauth_access_token_encrypted",
            "password",
            "user_id",
            "imap_host",
            "imap_port",
            "smtp_host",
        }
        assert forbidden.isdisjoint(set(mb.keys()))

    async def test_display_name_nullable_serialized_as_null(
        self,
        client: httpx.AsyncClient,
        api_key_on: str,
        super_admin: Any,
        make_mail_account: Callable[..., Any],
    ) -> None:
        acc = await make_mail_account(super_admin.id, "nulldn@example.com", display_name=None)
        mb = await self._one(client, api_key_on, acc.id)
        assert mb["display_name"] is None

    async def test_personal_mailbox_group_id_null(
        self,
        client: httpx.AsyncClient,
        api_key_on: str,
        super_admin: Any,
        make_mail_account: Callable[..., Any],
    ) -> None:
        """A personal mailbox (no team) surfaces ``group_id: null`` (ADR-0037 §2)."""
        acc = await make_mail_account(super_admin.id, "personal@example.com")
        assert acc.group_id is None
        mb = await self._one(client, api_key_on, acc.id)
        assert mb["group_id"] is None

    async def test_is_active_false_surfaced(
        self,
        client: httpx.AsyncClient,
        api_key_on: str,
        super_admin: Any,
        make_mail_account: Callable[..., Any],
        db_engine: AsyncEngine,
    ) -> None:
        """A worker-disabled mailbox (``is_active=false``, ADR-0033) is still
        listed, with ``is_active: false``."""
        acc = await make_mail_account(super_admin.id, "disabled@example.com")
        await _set_inactive(db_engine, acc.id)
        mb = await self._one(client, api_key_on, acc.id)
        assert mb["is_active"] is False

    async def test_canonical_dedup_two_accounts_one_email_one_min_id(
        self,
        client: httpx.AsyncClient,
        api_key_on: str,
        super_admin: Any,
        make_mail_account: Callable[..., Any],
        make_secondary_team_mailbox: Callable[..., Any],
    ) -> None:
        """Same ``LOWER(email)`` added by two teams → ONE canonical (``MIN(id)``)
        mailbox in the list; the non-canonical duplicate is absent (ADR-0037 §2).
        """
        canon = await make_mail_account(super_admin.id, "Shared@Example.com")
        dup = await make_secondary_team_mailbox(
            username="dup_mb_owner", group_name="DupTeam", email="shared@example.com"
        )
        assert canon.id < dup.id  # canonical = MIN(id)

        ids = [mb["id"] for mb in await self._all(client, api_key_on)]
        assert canon.id in ids, "canonical mailbox must be present"
        assert dup.id not in ids, "non-canonical duplicate must be absent"

    async def test_consistency_mailbox_ids_cover_message_mail_account_ids(
        self,
        client: httpx.AsyncClient,
        api_key_on: str,
        super_admin: Any,
        make_mail_account: Callable[..., Any],
        make_message: Callable[..., Any],
        make_secondary_team_mailbox: Callable[..., Any],
    ) -> None:
        """Every ``messages[].mail_account.id`` returned by ``GET /messages`` is
        present in ``GET /mailboxes`` — same ``list_canonical_account_ids`` set,
        so the CRM's message→mailbox join always resolves (ADR-0037 §4)."""
        acc_a = await make_mail_account(super_admin.id, "cons-a@example.com")
        acc_b = await make_secondary_team_mailbox(
            username="cons_b_u", group_name="ConsB", email="cons-b@example.com"
        )
        await make_message(acc_a.id, uid=1)
        await make_message(acc_b.id, uid=1)

        msgs = await client.get(
            "/api/external/messages", headers={"X-API-Key": api_key_on}, params={"limit": 200}
        )
        assert msgs.status_code == 200
        message_account_ids = {m["mail_account"]["id"] for m in msgs.json()["messages"]}
        assert message_account_ids, "expected at least one message"

        mailbox_ids = {mb["id"] for mb in await self._all(client, api_key_on)}
        assert (
            message_account_ids <= mailbox_ids
        ), "every message's mail_account.id must appear in /mailboxes"
