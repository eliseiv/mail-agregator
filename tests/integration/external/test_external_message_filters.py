"""Integration tests for the external message filters (ADR-0037 §3 → ADR-0039 §3).

``GET /api/external/messages`` carries two OPTIONAL query filters —
``mail_account_id`` and ``group_id`` — that NARROW the canonical mailbox set in
BOTH pagination modes (``order=asc`` forward / ``order=desc`` backward) without
changing the cursor semantics. ADR-0039 §3 **supersedes** the ADR-0037
mutual-exclusion: both filters are now REPEATABLE (``list[int]``) and
**AND-combinable**. The effective mailbox set is the intersection
``canonical ∩ (union of group_id) ∩ set(mail_account_id)``; an empty intersection
yields an EMPTY page (never 404, never 400); a missing/foreign/non-canonical id
simply does not appear in the intersection; a single value of either filter is
byte-for-byte backward compatible. ``field="filter"`` is NEVER returned any more
(the mutual-exclusion 400 was removed). An out-of-bounds ``<1`` value is no
longer a FastAPI 400 either — the router dropped the per-element ``ge`` bound —
so a ``mail_account_id=0`` just narrows to nothing (empty page). Omitting both
is byte-for-byte ADR-0029/0036 (BC).

Source of truth: ``docs/adr/ADR-0039-external-write-api.md`` §3 +
``docs/04-api-contracts.md`` §4d (filters table) +
``backend/app/external/{router,service}.py`` (``_resolve_account_ids`` = the
intersection; the router binds ``mail_account_id`` / ``group_id`` as
``list[int] | None`` with no ``ge``).

Only the HTTP boundary is exercised through the network — DB state is seeded
directly against real Postgres so the canonical-dedup ∩ filter resolution and
the keyset run against actual SQL (never a mock of our own code).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from shared.models import Group

pytestmark = pytest.mark.integration

_URL = "/api/external/messages"

_ASC_KEYS = {"messages", "next_since_id", "has_more"}
_DESC_KEYS = {"messages", "next_before_id", "has_more"}


async def _get(client: httpx.AsyncClient, key: str, **params: Any) -> dict[str, Any]:
    """GET the endpoint, assert 200, return the parsed body."""
    resp = await client.get(_URL, headers={"X-API-Key": key}, params=params)
    assert resp.status_code == 200, resp.text
    body: dict[str, Any] = resp.json()
    return body


async def _make_empty_group(db_engine: AsyncEngine, name: str) -> int:
    """Create a group with NO mailboxes; return its id (for the empty-team case)."""
    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with factory() as ses, ses.begin():
        g = Group(name=name, leader_user_id=None)
        ses.add(g)
        await ses.flush()
        await ses.refresh(g)
        return int(g.id)


# ===========================================================================
# 1. mail_account_id filter narrows in BOTH modes (ADR-0037 §3)
# ===========================================================================


class TestMailAccountFilter:
    @pytest.mark.parametrize("order", ["asc", "desc"])
    async def test_mail_account_id_narrows_to_that_mailbox(
        self,
        client: httpx.AsyncClient,
        api_key_on: str,
        super_admin: Any,
        make_mail_account: Callable[..., Any],
        make_message: Callable[..., Any],
        order: str,
    ) -> None:
        """Only the target mailbox's messages are returned; sibling-mailbox
        messages are excluded — identically for ``asc`` and ``desc``."""
        acc_a = await make_mail_account(super_admin.id, "fa@example.com")
        acc_b = await make_mail_account(super_admin.id, "fb@example.com")
        a1 = await make_message(acc_a.id, uid=1, subject="a1")
        a2 = await make_message(acc_a.id, uid=2, subject="a2")
        b1 = await make_message(acc_b.id, uid=1, subject="b1")

        body = await _get(client, api_key_on, order=order, mail_account_id=acc_a.id, limit=200)
        got = {m["id"] for m in body["messages"]}
        assert got == {a1.id, a2.id}, "filter must return ONLY acc_a's messages"
        assert b1.id not in got
        # Each returned row indeed belongs to the requested mailbox.
        assert all(m["mail_account"]["id"] == acc_a.id for m in body["messages"])

    async def test_filter_preserves_asc_cursor_semantics(
        self,
        client: httpx.AsyncClient,
        api_key_on: str,
        super_admin: Any,
        make_mail_account: Callable[..., Any],
        make_message: Callable[..., Any],
    ) -> None:
        """Under a ``mail_account_id`` filter the ``asc`` keyset still paginates
        normally: ``next_since_id`` = last id, ``has_more`` = page full, and a
        follow-up with the cursor returns the remaining rows with no dupes."""
        acc = await make_mail_account(super_admin.id, "fc@example.com")
        # Noise on a DIFFERENT mailbox — must never leak into the filtered page.
        other = await make_mail_account(super_admin.id, "fc-other@example.com")
        await make_message(other.id, uid=99)
        ids = [(await make_message(acc.id, uid=i, subject=f"s{i}")).id for i in range(1, 4)]

        page1 = await _get(client, api_key_on, mail_account_id=acc.id, since_id=0, limit=2)
        assert set(page1.keys()) == _ASC_KEYS
        assert [m["id"] for m in page1["messages"]] == ids[:2]
        assert page1["next_since_id"] == ids[1]
        assert page1["has_more"] is True

        page2 = await _get(
            client, api_key_on, mail_account_id=acc.id, since_id=page1["next_since_id"], limit=2
        )
        assert [m["id"] for m in page2["messages"]] == ids[2:]
        assert page2["next_since_id"] == ids[2]
        assert page2["has_more"] is False

    async def test_filter_preserves_desc_cursor_semantics(
        self,
        client: httpx.AsyncClient,
        api_key_on: str,
        super_admin: Any,
        make_mail_account: Callable[..., Any],
        make_message: Callable[..., Any],
    ) -> None:
        """Under the filter the ``desc`` reverse keyset is unchanged:
        ``next_before_id`` = ``min(id)`` of the batch, ``has_more`` = page full."""
        acc = await make_mail_account(super_admin.id, "fd@example.com")
        other = await make_mail_account(super_admin.id, "fd-other@example.com")
        await make_message(other.id, uid=99)
        ids = [(await make_message(acc.id, uid=i)).id for i in range(1, 4)]  # ascending ids

        page1 = await _get(client, api_key_on, order="desc", mail_account_id=acc.id, limit=2)
        assert set(page1.keys()) == _DESC_KEYS
        assert [m["id"] for m in page1["messages"]] == [ids[2], ids[1]], "newest-first"
        assert page1["next_before_id"] == ids[1]  # min(id) of the DESC batch
        assert page1["has_more"] is True

        page2 = await _get(
            client,
            api_key_on,
            order="desc",
            mail_account_id=acc.id,
            before_id=page1["next_before_id"],
            limit=2,
        )
        assert [m["id"] for m in page2["messages"]] == [ids[0]]
        assert page2["has_more"] is False


# ===========================================================================
# 2. group_id filter narrows in BOTH modes (ADR-0037 §3)
# ===========================================================================


class TestGroupFilter:
    @pytest.mark.parametrize("order", ["asc", "desc"])
    async def test_group_id_narrows_to_that_team(
        self,
        client: httpx.AsyncClient,
        api_key_on: str,
        make_message: Callable[..., Any],
        make_secondary_team_mailbox: Callable[..., Any],
        order: str,
    ) -> None:
        """Only messages of the requested team's mailboxes are returned; another
        team's messages are excluded — identically for ``asc`` and ``desc``."""
        acc_a = await make_secondary_team_mailbox(
            username="gf_a_u", group_name="GF-A", email="gf-a@example.com"
        )
        acc_b = await make_secondary_team_mailbox(
            username="gf_b_u", group_name="GF-B", email="gf-b@example.com"
        )
        a1 = await make_message(acc_a.id, uid=1)
        a2 = await make_message(acc_a.id, uid=2)
        b1 = await make_message(acc_b.id, uid=1)

        body = await _get(client, api_key_on, order=order, group_id=acc_a.group_id, limit=200)
        got = {m["id"] for m in body["messages"]}
        assert got == {a1.id, a2.id}, "filter must return ONLY team A's messages"
        assert b1.id not in got


# ===========================================================================
# 3. AND-combination — canonical ∩ (union group_id) ∩ set(mail_account_id) (ADR-0039 §3)
#    (the ADR-0037 mutual-exclusion is SUPERSEDED — both filters coexist)
# ===========================================================================


class TestAndCombination:
    @pytest.mark.parametrize("order", ["asc", "desc"])
    async def test_group_and_mail_account_intersect(
        self,
        client: httpx.AsyncClient,
        api_key_on: str,
        make_message: Callable[..., Any],
        make_mail_account: Callable[..., Any],
        make_secondary_team_mailbox: Callable[..., Any],
        order: str,
    ) -> None:
        """Both filters set → the INTERSECTION, not a 400. A mailbox that is in
        the requested team AND is the requested mail_account survives; a sibling
        mailbox of the SAME team is excluded by the mail_account filter, and a
        mailbox of ANOTHER team is excluded by the group filter."""
        acc_a1 = await make_secondary_team_mailbox(
            username="ac_a1", group_name="AC-A", email="ac-a1@example.com"
        )
        # Second mailbox of the SAME team A (same owner + group) — the group
        # filter would include it, but the mail_account filter must exclude it.
        acc_a2 = await make_mail_account(
            acc_a1.user_id, "ac-a2@example.com", group_id=acc_a1.group_id
        )
        acc_b = await make_secondary_team_mailbox(
            username="ac_b", group_name="AC-B", email="ac-b@example.com"
        )
        m_a1 = await make_message(acc_a1.id, uid=1)
        await make_message(acc_a2.id, uid=1)  # same team, excluded by mail_account
        await make_message(acc_b.id, uid=1)  # other team, excluded by group

        body = await _get(
            client,
            api_key_on,
            order=order,
            group_id=acc_a1.group_id,
            mail_account_id=acc_a1.id,
            limit=200,
        )
        got = {m["id"] for m in body["messages"]}
        assert got == {m_a1.id}, "only the mailbox in BOTH the team AND the id set survives"

    async def test_repeated_mail_account_ids_are_unioned_then_intersected(
        self,
        client: httpx.AsyncClient,
        api_key_on: str,
        super_admin: Any,
        make_mail_account: Callable[..., Any],
        make_message: Callable[..., Any],
    ) -> None:
        """``?mail_account_id=A&mail_account_id=B`` returns messages of BOTH A and
        B (the id set is a union); a third mailbox C is excluded."""
        acc_a = await make_mail_account(super_admin.id, "ra@example.com")
        acc_b = await make_mail_account(super_admin.id, "rb@example.com")
        acc_c = await make_mail_account(super_admin.id, "rc@example.com")
        a1 = await make_message(acc_a.id, uid=1)
        b1 = await make_message(acc_b.id, uid=1)
        await make_message(acc_c.id, uid=1)

        resp = await client.get(
            _URL,
            headers={"X-API-Key": api_key_on},
            params=[("mail_account_id", acc_a.id), ("mail_account_id", acc_b.id), ("limit", 200)],
        )
        assert resp.status_code == 200, resp.text
        got = {m["id"] for m in resp.json()["messages"]}
        assert got == {a1.id, b1.id}

    async def test_repeated_group_ids_are_unioned(
        self,
        client: httpx.AsyncClient,
        api_key_on: str,
        make_message: Callable[..., Any],
        make_secondary_team_mailbox: Callable[..., Any],
    ) -> None:
        """``?group_id=G1&group_id=G2`` returns messages of BOTH teams (union)."""
        acc_a = await make_secondary_team_mailbox(
            username="rg_a", group_name="RG-A", email="rg-a@example.com"
        )
        acc_b = await make_secondary_team_mailbox(
            username="rg_b", group_name="RG-B", email="rg-b@example.com"
        )
        a1 = await make_message(acc_a.id, uid=1)
        b1 = await make_message(acc_b.id, uid=1)

        resp = await client.get(
            _URL,
            headers={"X-API-Key": api_key_on},
            params=[("group_id", acc_a.group_id), ("group_id", acc_b.group_id), ("limit", 200)],
        )
        assert resp.status_code == 200, resp.text
        got = {m["id"] for m in resp.json()["messages"]}
        assert got == {a1.id, b1.id}

    async def test_disjoint_group_and_mail_account_yield_empty_page_not_400(
        self,
        client: httpx.AsyncClient,
        api_key_on: str,
        make_message: Callable[..., Any],
        make_secondary_team_mailbox: Callable[..., Any],
    ) -> None:
        """A mail_account of team A intersected with group_id of team B → the
        intersection is empty → an empty 200 page, NOT a 400 and NOT a 404
        (ADR-0039 §3 — the mutual-exclusion is gone)."""
        acc_a = await make_secondary_team_mailbox(
            username="dj_a", group_name="DJ-A", email="dj-a@example.com"
        )
        acc_b = await make_secondary_team_mailbox(
            username="dj_b", group_name="DJ-B", email="dj-b@example.com"
        )
        await make_message(acc_a.id, uid=1)
        await make_message(acc_b.id, uid=1)

        body = await _get(
            client,
            api_key_on,
            group_id=acc_b.group_id,
            mail_account_id=acc_a.id,
            limit=50,
        )
        assert body["messages"] == []
        assert body["has_more"] is False


class TestFieldFilterNeverReturned:
    async def test_both_filters_return_200_never_field_filter(
        self,
        client: httpx.AsyncClient,
        api_key_on: str,
        super_admin: Any,
        make_mail_account: Callable[..., Any],
        make_message: Callable[..., Any],
    ) -> None:
        """Both filters set → 200 (intersection), NOT the old 400 ``field=filter``.
        Regression guard: ``field="filter"`` must never appear in any response."""
        acc = await make_mail_account(super_admin.id, "nf@example.com")
        await make_message(acc.id, uid=1)
        resp = await client.get(
            _URL,
            headers={"X-API-Key": api_key_on},
            params={"mail_account_id": acc.id, "group_id": 1},
        )
        assert resp.status_code == 200, resp.text
        assert "field" not in str(resp.json()), "field=filter must never be returned any more"

    async def test_both_filters_on_empty_system_is_empty_200_not_400(
        self, client: httpx.AsyncClient, api_key_on: str
    ) -> None:
        """Both filters set on an empty system → an empty 200 page, not a 400.
        The old ADR-0037 pre-DB mutual-exclusion 400 is removed (ADR-0039 §3)."""
        resp = await client.get(
            _URL,
            headers={"X-API-Key": api_key_on},
            params={"mail_account_id": 999_999, "group_id": 888_888},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["messages"] == []
        assert body["has_more"] is False

    @pytest.mark.parametrize("bad", [0, -1])
    async def test_below_one_ids_no_longer_400_just_empty(
        self, client: httpx.AsyncClient, api_key_on: str, bad: int
    ) -> None:
        """The ADR-0037 per-element ``ge=1`` bound was dropped (ADR-0039 §3): a
        ``mail_account_id=0`` is no longer a FastAPI 400 — it simply never
        appears in the canonical intersection → an empty 200 page."""
        resp = await client.get(
            _URL, headers={"X-API-Key": api_key_on}, params={"mail_account_id": bad}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["messages"] == []


# ===========================================================================
# 4. Missing / foreign / non-canonical id → EMPTY page, not 404 (ADR-0037 §3)
# ===========================================================================


class TestEmptyPageNotFound:
    async def test_nonexistent_mail_account_id_empty_page_asc_keeps_cursor(
        self,
        client: httpx.AsyncClient,
        api_key_on: str,
        super_admin: Any,
        make_mail_account: Callable[..., Any],
        make_message: Callable[..., Any],
    ) -> None:
        """A ``mail_account_id`` that does not exist → empty page (NOT 404); the
        ``asc`` cursor does not move (``next_since_id`` == the incoming
        ``since_id``). Seeded data on ANOTHER mailbox proves emptiness is caused
        by the filter, not an empty DB."""
        acc = await make_mail_account(super_admin.id, "exists@example.com")
        await make_message(acc.id, uid=1)
        body = await _get(client, api_key_on, mail_account_id=999_999, since_id=7, limit=50)
        assert body["messages"] == []
        assert body["next_since_id"] == 7, "asc cursor must stay at the incoming since_id"
        assert body["has_more"] is False

    async def test_nonexistent_mail_account_id_empty_page_desc_next_before_null(
        self,
        client: httpx.AsyncClient,
        api_key_on: str,
        super_admin: Any,
        make_mail_account: Callable[..., Any],
        make_message: Callable[..., Any],
    ) -> None:
        acc = await make_mail_account(super_admin.id, "exists2@example.com")
        await make_message(acc.id, uid=1)
        body = await _get(client, api_key_on, order="desc", mail_account_id=999_999, limit=50)
        assert body["messages"] == []
        assert body["next_before_id"] is None, "desc empty page → next_before_id null"
        assert body["has_more"] is False

    async def test_non_canonical_mail_account_id_empty_page(
        self,
        client: httpx.AsyncClient,
        api_key_on: str,
        super_admin: Any,
        make_mail_account: Callable[..., Any],
        make_message: Callable[..., Any],
        make_secondary_team_mailbox: Callable[..., Any],
    ) -> None:
        """A mail_account_id that EXISTS but is the non-canonical duplicate
        (higher id for the same ``LOWER(email)``) is not in ``canonical_ids`` →
        empty page (its messages surface only via the canonical id)."""
        canon = await make_mail_account(super_admin.id, "Dup@Example.com")
        dup = await make_secondary_team_mailbox(
            username="nc_owner", group_name="NCTeam", email="dup@example.com"
        )
        assert canon.id < dup.id
        await make_message(dup.id, uid=1)  # message lives on the non-canonical row

        body = await _get(client, api_key_on, mail_account_id=dup.id, since_id=0, limit=50)
        assert body["messages"] == [], "non-canonical mailbox filter → empty page"
        assert body["has_more"] is False

    async def test_nonexistent_group_id_empty_page(
        self,
        client: httpx.AsyncClient,
        api_key_on: str,
        super_admin: Any,
        make_mail_account: Callable[..., Any],
        make_message: Callable[..., Any],
    ) -> None:
        acc = await make_mail_account(super_admin.id, "g-exists@example.com")
        await make_message(acc.id, uid=1)
        body = await _get(client, api_key_on, group_id=777_777, since_id=3, limit=50)
        assert body["messages"] == []
        assert body["next_since_id"] == 3
        assert body["has_more"] is False

    async def test_empty_group_no_mailboxes_empty_page(
        self,
        client: httpx.AsyncClient,
        api_key_on: str,
        super_admin: Any,
        make_mail_account: Callable[..., Any],
        make_message: Callable[..., Any],
        db_engine: AsyncEngine,
    ) -> None:
        """A group that EXISTS but has no mailboxes → empty page."""
        acc = await make_mail_account(super_admin.id, "eg@example.com")
        await make_message(acc.id, uid=1)
        empty_group_id = await _make_empty_group(db_engine, "EmptyTeam")
        body = await _get(client, api_key_on, group_id=empty_group_id, limit=50)
        assert body["messages"] == []
        assert body["has_more"] is False

    async def test_group_with_only_non_canonical_mailbox_empty_page(
        self,
        client: httpx.AsyncClient,
        api_key_on: str,
        super_admin: Any,
        make_mail_account: Callable[..., Any],
        make_message: Callable[..., Any],
        make_secondary_team_mailbox: Callable[..., Any],
    ) -> None:
        """A group whose only mailbox is the non-canonical duplicate → the
        canonical intersection is empty → empty page (ADR-0037 §3)."""
        canon = await make_mail_account(super_admin.id, "OnlyDup@Example.com")
        dup = await make_secondary_team_mailbox(
            username="odg_owner", group_name="OnlyDupGroup", email="onlydup@example.com"
        )
        assert canon.id < dup.id
        await make_message(dup.id, uid=1)
        body = await _get(client, api_key_on, group_id=dup.group_id, limit=50)
        assert body["messages"] == [], "all-non-canonical team → empty page"
        assert body["has_more"] is False


# ===========================================================================
# 6. Backward compatibility — no new params == ADR-0029/0036 (ADR-0037 §6)
# ===========================================================================


class TestBackwardCompatibility:
    async def test_no_filters_asc_returns_all_messages_unchanged(
        self,
        client: httpx.AsyncClient,
        api_key_on: str,
        seed_n_messages: Callable[..., Any],
    ) -> None:
        """Without the new filters the default (``asc``) response is unchanged:
        all messages, forward keys, id-ASC order (ADR-0029 BC)."""
        ids = await seed_n_messages(3)
        body = await _get(client, api_key_on)
        assert set(body.keys()) == _ASC_KEYS
        assert [m["id"] for m in body["messages"]] == ids
        assert body["has_more"] is False

    async def test_no_filters_desc_returns_all_messages_unchanged(
        self,
        client: httpx.AsyncClient,
        api_key_on: str,
        seed_n_messages: Callable[..., Any],
    ) -> None:
        """``order=desc`` without filters is unchanged (ADR-0036 BC): all
        messages newest-first, backward keys."""
        ids = await seed_n_messages(3)
        body = await _get(client, api_key_on, order="desc")
        assert set(body.keys()) == _DESC_KEYS
        assert [m["id"] for m in body["messages"]] == sorted(ids, reverse=True)
        assert body["has_more"] is False

    async def test_reply_endpoint_still_guarded_not_affected_by_filters(
        self, client: httpx.AsyncClient, api_key_on: str
    ) -> None:
        """The ADR-0035 reply route is untouched by the ADR-0037 GET filters:
        it still requires the key (401 without one) — a smoke check that adding
        query filters to ``GET /messages`` did not regress the reply POST."""
        resp = await client.post("/api/external/messages/1/reply", json={"body": "hi"})
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "not_authenticated"
