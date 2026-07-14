"""Integration tests for the external message filter (ADR-0039 §3, post-decommission).

``GET /api/external/messages`` carries ONE optional, REPEATABLE query filter —
``mail_account_id`` — that NARROWS the canonical mailbox set in BOTH pagination
modes (``order=asc`` forward / ``order=desc`` backward) without changing the cursor
semantics. The effective mailbox set is ``canonical ∩ set(mail_account_id)``; an
empty intersection yields an EMPTY page (never 404, never 400); a missing / foreign /
non-canonical id simply does not appear in the intersection; a single value is
byte-for-byte backward compatible. An out-of-bounds ``<1`` value is not a 400 either
(the router has no per-element ``ge`` bound) — it just narrows to nothing. Omitting
the filter is byte-for-byte ADR-0029/0036 (BC).

ADR-0044 §4 (phase A1): the second filter, ``group_id``, went away with teams/groups
— its tests (and the AND-combination / ``field="filter"`` cases that only existed
because there were TWO filters) went with it.

Source of truth: ``docs/adr/ADR-0039-external-write-api.md`` §3 +
``docs/04-api-contracts.md`` §4d (filters table) +
``backend/app/external/{router,service}.py`` (``_resolve_account_ids``; the router
binds ``mail_account_id`` as ``list[int] | None`` with no ``ge``).

Only the HTTP boundary is exercised through the network — DB state is seeded
directly against real Postgres so the canonical-dedup ∩ filter resolution and
the keyset run against actual SQL (never a mock of our own code).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest

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


class TestMailAccountFilter:
    @pytest.mark.parametrize("order", ["asc", "desc"])
    async def test_mail_account_id_narrows_to_that_mailbox(
        self,
        client: httpx.AsyncClient,
        api_key_on: str,
        owner: Any,
        make_mail_account: Callable[..., Any],
        make_message: Callable[..., Any],
        order: str,
    ) -> None:
        """Only the target mailbox's messages are returned; sibling-mailbox
        messages are excluded — identically for ``asc`` and ``desc``."""
        acc_a = await make_mail_account(owner.id, "fa@example.com")
        acc_b = await make_mail_account(owner.id, "fb@example.com")
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
        owner: Any,
        make_mail_account: Callable[..., Any],
        make_message: Callable[..., Any],
    ) -> None:
        """Under a ``mail_account_id`` filter the ``asc`` keyset still paginates
        normally: ``next_since_id`` = last id, ``has_more`` = page full, and a
        follow-up with the cursor returns the remaining rows with no dupes."""
        acc = await make_mail_account(owner.id, "fc@example.com")
        # Noise on a DIFFERENT mailbox — must never leak into the filtered page.
        other = await make_mail_account(owner.id, "fc-other@example.com")
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
        owner: Any,
        make_mail_account: Callable[..., Any],
        make_message: Callable[..., Any],
    ) -> None:
        """Under the filter the ``desc`` reverse keyset is unchanged:
        ``next_before_id`` = ``min(id)`` of the batch, ``has_more`` = page full."""
        acc = await make_mail_account(owner.id, "fd@example.com")
        other = await make_mail_account(owner.id, "fd-other@example.com")
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
# 2. Repeated ``mail_account_id`` values are UNIONed (ADR-0039 §3)
# ===========================================================================


class TestRepeatedMailAccountIds:
    async def test_repeated_mail_account_ids_are_unioned_then_intersected(
        self,
        client: httpx.AsyncClient,
        api_key_on: str,
        owner: Any,
        make_mail_account: Callable[..., Any],
        make_message: Callable[..., Any],
    ) -> None:
        """``?mail_account_id=A&mail_account_id=B`` returns messages of BOTH A and
        B (the id set is a union); a third mailbox C is excluded."""
        acc_a = await make_mail_account(owner.id, "ra@example.com")
        acc_b = await make_mail_account(owner.id, "rb@example.com")
        acc_c = await make_mail_account(owner.id, "rc@example.com")
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


# ===========================================================================
# 3. Out-of-bounds ids are an empty page, never a 400 (ADR-0039 §3)
# ===========================================================================


class TestBelowOneIdsAreNotA400:
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
        owner: Any,
        make_mail_account: Callable[..., Any],
        make_message: Callable[..., Any],
    ) -> None:
        """A ``mail_account_id`` that does not exist → empty page (NOT 404); the
        ``asc`` cursor does not move (``next_since_id`` == the incoming
        ``since_id``). Seeded data on ANOTHER mailbox proves emptiness is caused
        by the filter, not an empty DB."""
        acc = await make_mail_account(owner.id, "exists@example.com")
        await make_message(acc.id, uid=1)
        body = await _get(client, api_key_on, mail_account_id=999_999, since_id=7, limit=50)
        assert body["messages"] == []
        assert body["next_since_id"] == 7, "asc cursor must stay at the incoming since_id"
        assert body["has_more"] is False

    async def test_nonexistent_mail_account_id_empty_page_desc_next_before_null(
        self,
        client: httpx.AsyncClient,
        api_key_on: str,
        owner: Any,
        make_mail_account: Callable[..., Any],
        make_message: Callable[..., Any],
    ) -> None:
        acc = await make_mail_account(owner.id, "exists2@example.com")
        await make_message(acc.id, uid=1)
        body = await _get(client, api_key_on, order="desc", mail_account_id=999_999, limit=50)
        assert body["messages"] == []
        assert body["next_before_id"] is None, "desc empty page → next_before_id null"
        assert body["has_more"] is False

    async def test_non_canonical_mail_account_id_empty_page(
        self,
        client: httpx.AsyncClient,
        api_key_on: str,
        owner: Any,
        make_mail_account: Callable[..., Any],
        make_message: Callable[..., Any],
        make_secondary_owner_mailbox: Callable[..., Any],
    ) -> None:
        """A mail_account_id that EXISTS but is the non-canonical duplicate
        (higher id for the same ``LOWER(email)``) is not in ``canonical_ids`` →
        empty page (its messages surface only via the canonical id)."""
        canon = await make_mail_account(owner.id, "Dup@Example.com")
        dup = await make_secondary_owner_mailbox(username="nc_owner", email="dup@example.com")
        assert canon.id < dup.id
        await make_message(dup.id, uid=1)  # message lives on the non-canonical row

        body = await _get(client, api_key_on, mail_account_id=dup.id, since_id=0, limit=50)
        assert body["messages"] == [], "non-canonical mailbox filter → empty page"
        assert body["has_more"] is False


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
