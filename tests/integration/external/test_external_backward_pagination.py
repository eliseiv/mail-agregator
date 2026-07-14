"""Integration tests for the external PULL-API backward / newest-first mode (ADR-0036).

``GET /api/external/messages?order=desc[&before_id=][&limit=]`` — the CRM proxy
consumer walks the message history newest-first ("infinite feed"): ``order=desc``
without ``before_id`` returns the freshest N by ``id DESC``; ``before_id`` pages
back into older messages. The forward mode (``order=asc``/omitted, ADR-0029) is
byte-for-byte unchanged — this suite proves both that the new ``desc`` mode is
correct AND that the ``asc`` backward-compatibility is intact.

Source of truth: ``docs/adr/ADR-0036-external-backward-pagination.md`` +
``docs/04-api-contracts.md`` §4 (external read) +
``backend/app/external/{router,service,schemas}.py`` +
``backend/app/repositories/messages.py``.

The HTTP boundary is the only mocked seam — DB state is seeded directly against
real Postgres so the reverse keyset and canonical-dedup paths run
against actual SQL (never a mock of our own code). ``seed_n_messages`` seeds
``internal_date`` DESCENDING against ``id`` ASCENDING, so a naive
``ORDER BY internal_date`` would order rows the OPPOSITE way to the id-keyset —
that is exactly what makes the "``desc`` is over ``id`` not date" assertions
load-bearing (ADR-0036 §Decision / §Consequences).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest

pytestmark = pytest.mark.integration

_URL = "/api/external/messages"

_DESC_KEYS = {"messages", "next_before_id", "has_more"}
_ASC_KEYS = {"messages", "next_since_id", "has_more"}


async def _get(client: httpx.AsyncClient, key: str, **params: Any) -> dict[str, Any]:
    """GET the endpoint, assert 200, return the parsed body."""
    resp = await client.get(_URL, headers={"X-API-Key": key}, params=params)
    assert resp.status_code == 200, resp.text
    body: dict[str, Any] = resp.json()
    return body


# ===========================================================================
# 1. desc latest — order=desc, no before_id (ADR-0036 §2/§3)
# ===========================================================================


class TestBackwardLatest:
    async def test_latest_returns_freshest_n_by_id_desc(
        self,
        client: httpx.AsyncClient,
        api_key_on: str,
        seed_n_messages: Callable[..., Any],
    ) -> None:
        """``order=desc&limit=2`` on 5 messages → the two HIGHEST ids, newest-first.

        ``seed_n_messages`` gives ``id`` ASC while ``internal_date`` DESC, so the
        freshest by ``id`` (``ids[-1]``, ``ids[-2]``) are the OLDEST by date —
        proving the reverse keyset is over ``id`` (ADR-0036), not ``internal_date``.
        """
        ids = await seed_n_messages(5)  # ids ascending in insert order
        body = await _get(client, api_key_on, order="desc", limit=2)
        page_ids = [m["id"] for m in body["messages"]]
        assert page_ids == [ids[4], ids[3]], "latest page must be the top-2 ids, id DESC"
        assert page_ids == sorted(page_ids, reverse=True), "page not id DESC (newest-first)"

    async def test_latest_next_before_id_is_min_of_batch_and_has_more(
        self,
        client: httpx.AsyncClient,
        api_key_on: str,
        seed_n_messages: Callable[..., Any],
    ) -> None:
        """``next_before_id`` = ``min(id)`` of the batch (= last DESC element);
        ``has_more`` = page was full (``len == limit``)."""
        ids = await seed_n_messages(5)
        body = await _get(client, api_key_on, order="desc", limit=2)
        assert set(body.keys()) == _DESC_KEYS
        assert body["next_before_id"] == min(m["id"] for m in body["messages"])
        assert body["next_before_id"] == ids[3]  # last element of [id4, id3]
        assert body["has_more"] is True  # len == limit == 2

    async def test_latest_full_history_fits_has_more_false(
        self,
        client: httpx.AsyncClient,
        api_key_on: str,
        seed_n_messages: Callable[..., Any],
    ) -> None:
        """When ``limit`` exceeds the total, the whole history returns newest-first,
        ``has_more`` is ``false`` and ``next_before_id`` is the lowest id."""
        ids = await seed_n_messages(3)
        body = await _get(client, api_key_on, order="desc", limit=50)
        assert [m["id"] for m in body["messages"]] == [ids[2], ids[1], ids[0]]
        assert body["has_more"] is False  # 3 < 50
        assert body["next_before_id"] == ids[0]  # min(id) of the full batch

    async def test_latest_empty_system_returns_null_cursor(
        self,
        client: httpx.AsyncClient,
        api_key_on: str,
    ) -> None:
        """No messages at all → ``{messages: [], next_before_id: null, has_more: false}``."""
        body = await _get(client, api_key_on, order="desc", limit=50)
        assert body["messages"] == []
        assert body["next_before_id"] is None
        assert body["has_more"] is False


# ===========================================================================
# 2. desc older — order=desc + before_id (ADR-0036 §2/§3)
# ===========================================================================


class TestBackwardOlderPage:
    async def test_before_id_returns_only_strictly_older_ids_desc(
        self,
        client: httpx.AsyncClient,
        api_key_on: str,
        seed_n_messages: Callable[..., Any],
    ) -> None:
        """``before_id`` is a STRICT bound: only ``id < before_id``, ordered id DESC."""
        ids = await seed_n_messages(5)
        body = await _get(client, api_key_on, order="desc", before_id=ids[3], limit=50)
        got = [m["id"] for m in body["messages"]]
        assert got == [ids[2], ids[1], ids[0]], "only ids strictly below before_id, id DESC"
        assert ids[3] not in got and ids[4] not in got

    async def test_pagination_walks_full_history_to_the_end(
        self,
        client: httpx.AsyncClient,
        api_key_on: str,
        seed_n_messages: Callable[..., Any],
    ) -> None:
        """Feed iteration: latest page, then follow ``next_before_id`` until
        ``has_more`` is ``false`` — the union is the full history, newest-first,
        no dupes, no gaps (ADR-0036 §3 consumer loop)."""
        ids = await seed_n_messages(5)
        seen: list[int] = []
        # First screen: latest N (no before_id).
        body = await _get(client, api_key_on, order="desc", limit=2)
        seen.extend(m["id"] for m in body["messages"])
        # Scroll down by the cursor.
        for _ in range(10):  # generous loop guard
            if not body["has_more"]:
                break
            body = await _get(
                client, api_key_on, order="desc", before_id=body["next_before_id"], limit=2
            )
            seen.extend(m["id"] for m in body["messages"])
        assert seen == list(reversed(ids)), "walk must reproduce full history newest-first"
        assert len(seen) == len(set(seen)), "no duplicates across pages"

    async def test_before_id_below_all_ids_empty_batch_ends_pagination(
        self,
        client: httpx.AsyncClient,
        api_key_on: str,
        seed_n_messages: Callable[..., Any],
    ) -> None:
        """``before_id`` = the smallest id → no older rows: empty batch,
        ``next_before_id`` null, ``has_more`` false (end of history)."""
        ids = await seed_n_messages(3)
        body = await _get(client, api_key_on, order="desc", before_id=ids[0], limit=50)
        assert body["messages"] == []
        assert body["next_before_id"] is None
        assert body["has_more"] is False


# ===========================================================================
# 3. asc backward-compatibility (CRITICAL — ADR-0036 §7)
# ===========================================================================


class TestForwardBackwardCompat:
    async def test_order_omitted_equals_order_asc_byte_for_byte(
        self,
        client: httpx.AsyncClient,
        api_key_on: str,
        seed_n_messages: Callable[..., Any],
    ) -> None:
        """Omitting ``order`` and passing ``order=asc`` must produce IDENTICAL
        forward responses (ADR-0029 default preserved, ADR-0036 §7 BC)."""
        await seed_n_messages(5)
        omitted = await _get(client, api_key_on, since_id=0, limit=3)
        explicit = await _get(client, api_key_on, order="asc", since_id=0, limit=3)
        assert omitted == explicit

    async def test_asc_keyset_id_ascending_with_since_cursor_and_has_more(
        self,
        client: httpx.AsyncClient,
        api_key_on: str,
        seed_n_messages: Callable[..., Any],
    ) -> None:
        """``order=asc`` = ADR-0029 forward keyset: id ASC, ``next_since_id`` =
        ``max(id)`` of the batch, ``has_more`` = ``len == limit``."""
        ids = await seed_n_messages(5)
        body = await _get(client, api_key_on, order="asc", since_id=0, limit=2)
        page_ids = [m["id"] for m in body["messages"]]
        assert page_ids == [ids[0], ids[1]], "forward page = lowest ids, id ASC"
        assert page_ids == sorted(page_ids)
        assert body["next_since_id"] == max(page_ids)
        assert body["has_more"] is True

    async def test_asc_response_has_no_next_before_id(
        self,
        client: httpx.AsyncClient,
        api_key_on: str,
        seed_n_messages: Callable[..., Any],
    ) -> None:
        """The ``asc`` envelope carries ``next_since_id`` and NEVER
        ``next_before_id`` (ADR-0036 §3 — each cursor only in its own mode)."""
        await seed_n_messages(2)
        for params in ({"since_id": 0, "limit": 50}, {"order": "asc", "since_id": 0, "limit": 50}):
            body = await _get(client, api_key_on, **params)
            assert set(body.keys()) == _ASC_KEYS
            assert "next_before_id" not in body

    async def test_desc_response_has_no_next_since_id(
        self,
        client: httpx.AsyncClient,
        api_key_on: str,
        seed_n_messages: Callable[..., Any],
    ) -> None:
        """The ``desc`` envelope carries ``next_before_id`` and NEVER
        ``next_since_id`` (ADR-0036 §3)."""
        await seed_n_messages(2)
        body = await _get(client, api_key_on, order="desc", limit=50)
        assert set(body.keys()) == _DESC_KEYS
        assert "next_since_id" not in body


# ===========================================================================
# 4. Deterministic 400 validation — normalised ``field`` (ADR-0036 §5)
# ===========================================================================


class TestValidationDeterministicField:
    """Mode co-existence errors (ADR-0036 §5). Two envelope families:

    - service-level ``ValidationError`` → 400 with a NORMALISED top-level
      ``error.field`` (the deterministic check order guarantees which one wins);
    - FastAPI ``RequestValidationError`` (bounds/type) → 400 ``validation_error``
      with ``error.details.errors[]`` and NO top-level ``field``.
    """

    async def _err(self, client: httpx.AsyncClient, key: str, **params: Any) -> dict[str, Any]:
        resp = await client.get(_URL, headers={"X-API-Key": key}, params=params)
        assert resp.status_code == 400, f"{params} -> {resp.status_code}: {resp.text}"
        err: dict[str, Any] = resp.json()["error"]
        assert err["code"] == "validation_error", err
        return err

    # --- service-level normalised field ------------------------------------

    async def test_order_invalid_field_is_order(
        self, client: httpx.AsyncClient, api_key_on: str
    ) -> None:
        err = await self._err(client, api_key_on, order="sideways")
        assert err["field"] == "order"

    async def test_before_id_with_order_asc_field_is_before_id(
        self, client: httpx.AsyncClient, api_key_on: str
    ) -> None:
        # Both the explicit asc and the omitted-order (default asc) paths reject
        # before_id with field=before_id (ADR-0036 §5 step 2).
        for params in ({"order": "asc", "before_id": 10}, {"before_id": 10}):
            err = await self._err(client, api_key_on, **params)
            assert err["field"] == "before_id", params

    async def test_since_id_with_order_desc_field_is_since_id(
        self, client: httpx.AsyncClient, api_key_on: str
    ) -> None:
        err = await self._err(client, api_key_on, order="desc", since_id=5)
        assert err["field"] == "since_id"

    async def test_before_id_zero_lower_bound_field_is_before_id(
        self, client: httpx.AsyncClient, api_key_on: str
    ) -> None:
        # order=desc so it clears the mode-mismatch check and reaches the
        # before_id < 1 lower-bound check (ADR-0036 §5 step 4) -> field=before_id.
        err = await self._err(client, api_key_on, order="desc", before_id=0)
        assert err["field"] == "before_id"

    # --- FastAPI bound/type errors: 400 validation_error, no normalised field ---

    async def test_nonnumeric_params_are_400_validation_error_without_field(
        self, client: httpx.AsyncClient, api_key_on: str
    ) -> None:
        for params in ({"since_id": "abc"}, {"before_id": "xyz"}, {"limit": "many"}):
            resp = await client.get(_URL, headers={"X-API-Key": api_key_on}, params=params)
            assert resp.status_code == 400, f"{params} -> {resp.status_code}: {resp.text}"
            err = resp.json()["error"]
            assert err["code"] == "validation_error", err
            # FastAPI-level errors surface loc in details.errors, not a top field.
            assert "field" not in err, err
            assert err["details"]["errors"], err

    async def test_since_id_negative_is_400_validation_error(
        self, client: httpx.AsyncClient, api_key_on: str
    ) -> None:
        resp = await client.get(_URL, headers={"X-API-Key": api_key_on}, params={"since_id": -1})
        assert resp.status_code == 400, resp.text
        assert resp.json()["error"]["code"] == "validation_error"

    async def test_limit_over_max_is_400_validation_error(
        self, client: httpx.AsyncClient, api_key_on: str
    ) -> None:
        resp = await client.get(_URL, headers={"X-API-Key": api_key_on}, params={"limit": 201})
        assert resp.status_code == 400, resp.text
        assert resp.json()["error"]["code"] == "validation_error"

    async def test_limit_zero_is_400_validation_error(
        self, client: httpx.AsyncClient, api_key_on: str
    ) -> None:
        resp = await client.get(_URL, headers={"X-API-Key": api_key_on}, params={"limit": 0})
        assert resp.status_code == 400, resp.text
        assert resp.json()["error"]["code"] == "validation_error"


# ===========================================================================
# 5. Canonical-dedup in desc (parity with forward — ADR-0036 §2)
# ===========================================================================


class TestBackwardCanonicalDedup:
    async def test_desc_canonical_dedup_two_accounts_one_email_one_copy(
        self,
        client: httpx.AsyncClient,
        api_key_on: str,
        owner: Any,
        make_mail_account: Callable[..., Any],
        make_message: Callable[..., Any],
        make_secondary_owner_mailbox: Callable[..., Any],
    ) -> None:
        """Two mail_accounts sharing ``LOWER(email)`` (one mailbox, two teams):
        the ``desc`` mode applies the SAME canonical (``MIN(id)``) dedup as
        forward — only the canonical account's message is returned (ADR-0036 §2).
        """
        acc_canon = await make_mail_account(owner.id, "Shared@Example.com")
        acc_dup = await make_secondary_owner_mailbox(
            username="dup_owner_desc", email="shared@example.com"
        )
        assert acc_canon.id < acc_dup.id  # canonical = MIN(id)
        m_canon = await make_message(acc_canon.id, uid=1, subject="dup-mail")
        m_dup = await make_message(acc_dup.id, uid=1, subject="dup-mail")

        body = await _get(client, api_key_on, order="desc", limit=200)
        ids = [m["id"] for m in body["messages"]]
        assert m_canon.id in ids, "canonical account's message must be returned in desc"
        assert m_dup.id not in ids, "non-canonical duplicate must NOT be returned in desc"
        account_ids = {m["mail_account"]["id"] for m in body["messages"]}
        assert acc_dup.id not in account_ids


# ===========================================================================
# 6. id-gaps from retention (ADR-0036 §Consequences)
# ===========================================================================


class TestBackwardIdGaps:
    async def test_desc_skips_deleted_middle_id(
        self,
        client: httpx.AsyncClient,
        api_key_on: str,
        db_engine: Any,
        seed_n_messages: Callable[..., Any],
    ) -> None:
        """A retention-deleted middle message leaves an id-gap; the reverse
        keyset ``id < before_id ORDER BY id DESC`` must skip it without crashing
        or dropping neighbours (ADR-0036 §Consequences / id-gaps)."""
        from sqlalchemy import delete
        from sqlalchemy.ext.asyncio import async_sessionmaker

        from shared.models import Message

        ids = await seed_n_messages(5)
        victim = ids[2]
        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as ses, ses.begin():
            await ses.execute(delete(Message).where(Message.id == victim))

        # Latest page over the gap.
        body = await _get(client, api_key_on, order="desc", limit=50)
        got = [m["id"] for m in body["messages"]]
        assert got == [i for i in reversed(ids) if i != victim]
        assert victim not in got

        # Older page whose before_id straddles the gap (id < ids[3] skips victim).
        older = await _get(client, api_key_on, order="desc", before_id=ids[3], limit=50)
        older_ids = [m["id"] for m in older["messages"]]
        assert older_ids == [ids[1], ids[0]], "gap at ids[2] is skipped, neighbours intact"
        assert victim not in older_ids


# ===========================================================================
# 7. Forward-GET regression (ADR-0029 not broken by the desc addition)
# ===========================================================================


class TestForwardRegression:
    async def test_forward_defaults_unchanged_after_desc_addition(
        self,
        client: httpx.AsyncClient,
        api_key_on: str,
        seed_n_messages: Callable[..., Any],
    ) -> None:
        """No params → ADR-0029 forward defaults (since_id=0, limit=50): full
        history id ASC, ``has_more`` false, forward cursor field only."""
        ids = await seed_n_messages(3)
        resp = await client.get(_URL, headers={"X-API-Key": api_key_on})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert [m["id"] for m in body["messages"]] == ids  # id ASC
        assert body["next_since_id"] == ids[-1]
        assert body["has_more"] is False  # 3 < 50
        assert set(body.keys()) == _ASC_KEYS
