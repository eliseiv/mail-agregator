"""Integration tests for the external PULL-API (ADR-0029).

``GET /api/external/messages?since_id=&limit=`` — a B2B partner incrementally
pulls ALL system messages with an API key (``X-API-Key`` / ``Authorization:
Bearer``) and a keyset cursor over ``messages.id``.

Source of truth: ``docs/adr/ADR-0029`` + ``docs/05-modules.md`` §21
qa_test_matrix + ``backend/app/external/{router,service,schemas}.py``.

The HTTP boundary is the only thing exercised through the network — DB state is
seeded directly (real Postgres) so the keyset and canonical-dedup paths run
against actual SQL, never a mock of our own code.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from tests.integration.external.conftest import TEST_API_KEY

pytestmark = pytest.mark.integration

_URL = "/api/external/messages"


# ===========================================================================
# 1. Auth (ADR-0029 §4)
# ===========================================================================


class TestAuth:
    async def test_valid_x_api_key_returns_200(
        self, client: httpx.AsyncClient, api_key_on: str
    ) -> None:
        resp = await client.get(_URL, headers={"X-API-Key": api_key_on})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert set(body.keys()) == {"messages", "next_since_id", "has_more"}

    async def test_valid_bearer_returns_200(
        self, client: httpx.AsyncClient, api_key_on: str
    ) -> None:
        resp = await client.get(_URL, headers={"Authorization": f"Bearer {api_key_on}"})
        assert resp.status_code == 200, resp.text

    async def test_bearer_case_insensitive_scheme(
        self, client: httpx.AsyncClient, api_key_on: str
    ) -> None:
        resp = await client.get(_URL, headers={"Authorization": f"bearer {api_key_on}"})
        assert resp.status_code == 200, resp.text

    async def test_x_api_key_takes_priority_over_bearer(
        self, client: httpx.AsyncClient, api_key_on: str
    ) -> None:
        # X-API-Key is correct, Bearer is garbage -> still 200 (X-API-Key wins).
        resp = await client.get(
            _URL,
            headers={"X-API-Key": api_key_on, "Authorization": "Bearer WRONG"},
        )
        assert resp.status_code == 200, resp.text

    async def test_wrong_key_returns_401(self, client: httpx.AsyncClient, api_key_on: str) -> None:
        resp = await client.get(_URL, headers={"X-API-Key": "wrong-key"})
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "not_authenticated"

    async def test_wrong_bearer_returns_401(
        self, client: httpx.AsyncClient, api_key_on: str
    ) -> None:
        resp = await client.get(_URL, headers={"Authorization": "Bearer wrong-key"})
        assert resp.status_code == 401

    async def test_no_key_returns_401(self, client: httpx.AsyncClient, api_key_on: str) -> None:
        resp = await client.get(_URL)
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "not_authenticated"

    async def test_malformed_authorization_header_returns_401(
        self, client: httpx.AsyncClient, api_key_on: str
    ) -> None:
        # Non-Bearer scheme / no token -> treated as missing key.
        for hdr in ("Basic abc", "Bearer", "Bearer    ", api_key_on):
            resp = await client.get(_URL, headers={"Authorization": hdr})
            assert resp.status_code == 401, f"header {hdr!r} -> {resp.status_code}"

    async def test_feature_off_any_key_returns_401(
        self, client: httpx.AsyncClient, set_external_api_key: Callable[[str], None]
    ) -> None:
        # EXTERNAL_API_KEY="" => feature disabled; indistinguishable from a
        # wrong key (opaque 401, config never disclosed) — ADR-0029 §4.
        set_external_api_key("")
        # Even sending the "test key" must NOT authenticate when feature is off.
        for headers in (
            {},
            {"X-API-Key": TEST_API_KEY},
            {"Authorization": f"Bearer {TEST_API_KEY}"},
            {"X-API-Key": ""},
        ):
            resp = await client.get(_URL, headers=headers)
            assert resp.status_code == 401, f"{headers} -> {resp.status_code}"
            assert resp.json()["error"]["code"] == "not_authenticated"

    async def test_empty_string_key_when_feature_on_returns_401(
        self, client: httpx.AsyncClient, api_key_on: str
    ) -> None:
        # An empty X-API-Key header must NOT match the configured key.
        resp = await client.get(_URL, headers={"X-API-Key": ""})
        assert resp.status_code == 401


class TestAuthRedaction:
    async def test_api_key_and_headers_not_in_logs(
        self,
        client: httpx.AsyncClient,
        api_key_on: str,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """The key / Authorization / X-API-Key must never appear in logs.

        We drive both a success and a failure path (each emits a structlog
        line) and assert the secret value is absent from captured stdout.
        ``EXTERNAL_API_KEY`` / ``X-API-Key`` / ``Authorization`` are on the
        redact-list (``shared/logging.py``).
        """
        # success path
        ok = await client.get(_URL, headers={"X-API-Key": api_key_on})
        assert ok.status_code == 200
        # failure path with a distinctive wrong secret
        distinctive_wrong = "SECRET_SHOULD_NOT_LEAK_12345"
        bad = await client.get(_URL, headers={"Authorization": f"Bearer {distinctive_wrong}"})
        assert bad.status_code == 401

        out = capsys.readouterr().out
        # The real configured key must not be logged anywhere.
        assert api_key_on not in out, "configured EXTERNAL_API_KEY leaked into logs"
        # The provided wrong secret must not be logged either.
        assert distinctive_wrong not in out, "provided key leaked into logs"


class TestRateLimitBeforeAuth:
    async def test_rate_limit_consumed_before_auth(
        self, client: httpx.AsyncClient, api_key_on: str
    ) -> None:
        """Exhausting the per-IP limit returns 429 + Retry-After WITHOUT the
        key being checked (rate-limit consume is FIRST — ADR-0029 §4).

        LIMIT_EXTERNAL_API = 120 / 60 s per IP. We send WRONG keys (so if auth
        ran first they'd all be 401); once the budget is gone the response must
        flip to 429 regardless of the (still wrong) key.

        The limiter keys on the client IP; under ASGITransport every request
        otherwise shares ``0.0.0.0``, so a sibling test could pre-consume the
        window. We pin a unique ``X-Forwarded-For`` (trusted by ``client_ip``)
        so this test owns a private, deterministic budget.
        """
        unique_ip = "203.0.113.77"  # TEST-NET-3, never a real sibling-test IP
        headers = {"X-API-Key": "still-wrong", "X-Forwarded-For": unique_ip}
        statuses: list[int] = []
        retry_after: int | None = None
        # 120 capacity -> the 121st is the first to exceed.
        for _ in range(130):
            r = await client.get(_URL, headers=headers)
            statuses.append(r.status_code)
            if r.status_code == 429:
                retry_after = int(r.headers["retry-after"])
                break
        assert 429 in statuses, f"never hit 429 in {len(statuses)} reqs: last={statuses[-5:]}"
        # All pre-429 responses were 401 (wrong key) — auth still ran for those,
        # but the 429 short-circuits before the key compare.
        assert set(statuses[:-1]) == {401}
        assert retry_after is not None and retry_after > 0


class TestRuntimeRateLimitOverride:
    """``EXTERNAL_API_RATE_LIMIT_PER_MINUTE`` tunes the per-IP cap at
    consume-time (ADR-0029 §1/§4 — same override pattern as
    ``WEBHOOK_TEST_LIMIT``).

    The router builds the runtime :class:`Limit` from
    ``settings.EXTERNAL_API_RATE_LIMIT_PER_MINUTE`` on every request, so an
    operator can retune the cap without a code redeploy. The window stays a
    fixed 60 s; only ``capacity`` moves.

    Each test pins a unique ``X-Forwarded-For`` (trusted by ``client_ip``) so
    it owns a private, deterministic per-IP budget — sibling tests sharing the
    ASGI ``0.0.0.0`` default must not pre-consume the window. Redis is FLUSHDB'd
    per test (autouse ``_redis_flush``) so the counter starts clean.
    """

    async def test_override_to_5_sixth_request_same_ip_returns_429_with_retry_after(
        self,
        client: httpx.AsyncClient,
        api_key_on: str,
        set_external_rate_limit: Callable[[int], None],
    ) -> None:
        """With the cap lowered to 5, the first 5 GETs from one IP pass (200)
        and the 6th flips to 429 + a positive ``Retry-After``.

        We send the VALID key so the only thing that can reject is the rate
        limit (auth would 200) — proving the override drives the 429.
        """
        set_external_rate_limit(5)
        ip = "203.0.113.5"  # TEST-NET-3 — private to this test
        headers = {"X-API-Key": api_key_on, "X-Forwarded-For": ip}

        # First 5 within budget -> 200.
        for i in range(5):
            r = await client.get(_URL, headers=headers)
            assert (
                r.status_code == 200
            ), f"req {i + 1}/5 expected 200, got {r.status_code}: {r.text}"

        # 6th exceeds the capacity-5 window -> 429 + Retry-After.
        sixth = await client.get(_URL, headers=headers)
        assert sixth.status_code == 429, f"6th expected 429, got {sixth.status_code}: {sixth.text}"
        assert sixth.json()["error"]["code"] == "rate_limited"
        retry_after = int(sixth.headers["retry-after"])
        assert 0 < retry_after <= 60, f"Retry-After out of the 60 s window: {retry_after}"

    async def test_override_window_is_per_ip_not_global(
        self,
        client: httpx.AsyncClient,
        api_key_on: str,
        set_external_rate_limit: Callable[[int], None],
    ) -> None:
        """The cap-5 budget is per client IP: a SECOND IP is unaffected after
        the first IP is exhausted (the limiter keys on ``ip:<ip>``)."""
        set_external_rate_limit(5)
        ip_a = "203.0.113.10"
        ip_b = "203.0.113.11"

        # Exhaust IP A (5 ok, 6th 429).
        for _ in range(5):
            assert (
                await client.get(_URL, headers={"X-API-Key": api_key_on, "X-Forwarded-For": ip_a})
            ).status_code == 200
        assert (
            await client.get(_URL, headers={"X-API-Key": api_key_on, "X-Forwarded-For": ip_a})
        ).status_code == 429

        # IP B still has its full, independent budget.
        first_b = await client.get(_URL, headers={"X-API-Key": api_key_on, "X-Forwarded-For": ip_b})
        assert first_b.status_code == 200, first_b.text

    async def test_default_120_allows_six_requests_from_one_ip(
        self,
        client: httpx.AsyncClient,
        api_key_on: str,
    ) -> None:
        """At the default cap (120/min) six back-to-back GETs from one IP all
        pass (200) — no 429. Asserts the override defaults to a generous cap
        and does NOT spuriously throttle normal pulls.

        ``api_key_on`` does NOT touch ``EXTERNAL_API_RATE_LIMIT_PER_MINUTE``, so
        the ambient default (120) is in force; we still pin a unique IP for a
        clean window.
        """
        from shared.config import get_settings

        assert (
            get_settings().EXTERNAL_API_RATE_LIMIT_PER_MINUTE == 120
        ), "this test asserts default-cap behaviour; ambient env overrode it"
        ip = "203.0.113.120"
        headers = {"X-API-Key": api_key_on, "X-Forwarded-For": ip}
        for i in range(6):
            r = await client.get(_URL, headers=headers)
            assert (
                r.status_code == 200
            ), f"req {i + 1}/6 expected 200, got {r.status_code}: {r.text}"


# ===========================================================================
# 2. Pagination / keyset (ADR-0029 §1)
# ===========================================================================


class TestPagination:
    async def _get(self, client: httpx.AsyncClient, key: str, **params: Any) -> dict[str, Any]:
        resp = await client.get(_URL, headers={"X-API-Key": key}, params=params)
        assert resp.status_code == 200, resp.text
        body: dict[str, Any] = resp.json()
        return body

    async def test_first_page_id_asc_with_next_cursor_and_has_more(
        self,
        client: httpx.AsyncClient,
        api_key_on: str,
        seed_n_messages: Callable[..., Any],
    ) -> None:
        ids = await seed_n_messages(5)  # ids ascending in insert order
        body = await self._get(client, api_key_on, since_id=0, limit=2)
        page_ids = [m["id"] for m in body["messages"]]
        assert page_ids == sorted(page_ids), "page not id ASC"
        assert page_ids == ids[:2]
        assert body["next_since_id"] == max(page_ids)
        assert body["has_more"] is True

    async def test_keyset_increment_no_dupes_no_gaps(
        self,
        client: httpx.AsyncClient,
        api_key_on: str,
        seed_n_messages: Callable[..., Any],
    ) -> None:
        ids = await seed_n_messages(5)
        seen: list[int] = []
        cursor = 0
        for _ in range(10):  # generous loop guard
            body = await self._get(client, api_key_on, since_id=cursor, limit=2)
            batch = [m["id"] for m in body["messages"]]
            seen.extend(batch)
            cursor = body["next_since_id"]
            if not body["has_more"]:
                break
        # Full set, in order, no duplicates, no gaps.
        assert seen == ids
        assert len(seen) == len(set(seen))

    async def test_empty_tail_keeps_cursor_and_has_more_false(
        self,
        client: httpx.AsyncClient,
        api_key_on: str,
        seed_n_messages: Callable[..., Any],
    ) -> None:
        ids = await seed_n_messages(3)
        last = max(ids)
        body = await self._get(client, api_key_on, since_id=last, limit=50)
        assert body["messages"] == []
        assert body["next_since_id"] == last  # cursor does not move
        assert body["has_more"] is False

    async def test_new_message_appears_with_id_above_cursor(
        self,
        client: httpx.AsyncClient,
        api_key_on: str,
        owner: Any,
        make_mail_account: Callable[..., Any],
        make_message: Callable[..., Any],
    ) -> None:
        """A message inserted AFTER the cursor surfaces even when its
        ``internal_date`` is EARLIER than already-pulled rows (keyset is over
        ``id``, not date — late internal_date is not lost). ADR-0029 §1."""
        acc = await make_mail_account(owner.id, "late@example.com")
        old = await make_message(acc.id, uid=1, internal_date=datetime.now(UTC), body_text="first")
        body = await self._get(client, api_key_on, since_id=0, limit=50)
        assert [m["id"] for m in body["messages"]] == [old.id]
        cursor = body["next_since_id"]

        # New row, but with an OLDER internal_date than ``old``.
        new = await make_message(
            acc.id,
            uid=2,
            internal_date=datetime.now(UTC) - timedelta(days=10),
            body_text="second",
        )
        assert new.id > cursor
        body2 = await self._get(client, api_key_on, since_id=cursor, limit=50)
        assert [m["id"] for m in body2["messages"]] == [new.id]

    async def test_deletion_creates_id_gap_keyset_survives(
        self,
        client: httpx.AsyncClient,
        api_key_on: str,
        db_engine: Any,
        seed_n_messages: Callable[..., Any],
    ) -> None:
        """Deleting a middle message (retention) leaves an id-gap; the keyset
        must not crash nor skip the neighbours. ADR-0029 §1 / matrix."""
        from sqlalchemy import delete
        from sqlalchemy.ext.asyncio import async_sessionmaker

        from shared.models import Message

        ids = await seed_n_messages(5)
        victim = ids[2]
        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as ses, ses.begin():
            await ses.execute(delete(Message).where(Message.id == victim))

        body = await self._get(client, api_key_on, since_id=0, limit=50)
        got = [m["id"] for m in body["messages"]]
        assert got == [i for i in ids if i != victim]
        assert victim not in got

    async def test_defaults_since_id_0_limit_50(
        self,
        client: httpx.AsyncClient,
        api_key_on: str,
        seed_n_messages: Callable[..., Any],
    ) -> None:
        ids = await seed_n_messages(3)
        # No query params -> since_id default 0, limit default 50.
        resp = await client.get(_URL, headers={"X-API-Key": api_key_on})
        assert resp.status_code == 200
        body = resp.json()
        assert [m["id"] for m in body["messages"]] == ids
        assert body["has_more"] is False  # 3 < 50

    async def test_limit_over_max_returns_400(
        self, client: httpx.AsyncClient, api_key_on: str
    ) -> None:
        resp = await client.get(_URL, headers={"X-API-Key": api_key_on}, params={"limit": 201})
        assert resp.status_code in (400, 422)

    async def test_limit_zero_returns_400(self, client: httpx.AsyncClient, api_key_on: str) -> None:
        resp = await client.get(_URL, headers={"X-API-Key": api_key_on}, params={"limit": 0})
        assert resp.status_code in (400, 422)

    async def test_negative_since_id_returns_400(
        self, client: httpx.AsyncClient, api_key_on: str
    ) -> None:
        resp = await client.get(_URL, headers={"X-API-Key": api_key_on}, params={"since_id": -1})
        assert resp.status_code in (400, 422)

    async def test_limit_at_max_200_ok(self, client: httpx.AsyncClient, api_key_on: str) -> None:
        resp = await client.get(_URL, headers={"X-API-Key": api_key_on}, params={"limit": 200})
        assert resp.status_code == 200


# ===========================================================================
# 3. Visibility / canonical-dedup (CRITICAL — ADR-0029 §5)
# ===========================================================================


class TestVisibilityAndCanonicalDedup:
    async def test_messages_of_all_teams_are_visible(
        self,
        client: httpx.AsyncClient,
        api_key_on: str,
        owner: Any,
        make_mail_account: Callable[..., Any],
        make_message: Callable[..., Any],
        make_secondary_owner_mailbox: Callable[..., Any],
    ) -> None:
        """External pull is owner-wide: messages of DIFFERENT teams /
        owners / groups are ALL returned (no per-group filter). ADR-0029 §5."""
        # Team A: the super-admin's own mailbox.
        acc_a = await make_mail_account(owner.id, "team-a@example.com")
        # Team B: a separate user + group + mailbox (one transaction).
        acc_b = await make_secondary_owner_mailbox(
            username="teamb_user", email="team-b@example.com"
        )
        m_a = await make_message(acc_a.id, uid=1, subject="A")
        m_b = await make_message(acc_b.id, uid=1, subject="B")

        resp = await client.get(_URL, headers={"X-API-Key": api_key_on})
        assert resp.status_code == 200
        got = {m["id"] for m in resp.json()["messages"]}
        assert {m_a.id, m_b.id} <= got, "messages from both teams must be visible"

    async def test_canonical_dedup_two_accounts_one_email_one_copy(
        self,
        client: httpx.AsyncClient,
        api_key_on: str,
        owner: Any,
        make_mail_account: Callable[..., Any],
        make_message: Callable[..., Any],
        make_secondary_owner_mailbox: Callable[..., Any],
    ) -> None:
        """Two mail_accounts with the SAME ``LOWER(email)`` (one mailbox added
        by two teams), each holding a message — external pull returns ONE copy:
        the message of the canonical (``MIN(id)``) account; the duplicate is
        absent. Consistent with the owner inbox. ADR-0029 §5 (CRITICAL).
        """
        # Same email, different case -> LOWER(email) collides. Canonical is the
        # smaller mail_accounts.id (the super-admin's, created first).
        acc_canon = await make_mail_account(owner.id, "Shared@Example.com")
        acc_dup = await make_secondary_owner_mailbox(
            username="dup_owner", email="shared@example.com"
        )
        assert acc_canon.id < acc_dup.id  # canonical = MIN(id)

        m_canon = await make_message(acc_canon.id, uid=1, subject="dup-mail")
        m_dup = await make_message(acc_dup.id, uid=1, subject="dup-mail")

        resp = await client.get(_URL, headers={"X-API-Key": api_key_on})
        assert resp.status_code == 200
        ids = [m["id"] for m in resp.json()["messages"]]
        assert m_canon.id in ids, "canonical account's message must be returned"
        assert m_dup.id not in ids, "non-canonical duplicate must NOT be returned"

        # Each returned mail_account.id belongs to the canonical set only.
        account_ids = {m["mail_account"]["id"] for m in resp.json()["messages"]}
        assert acc_dup.id not in account_ids


# ===========================================================================
# 4. Content / contract (ADR-0029 §2/§3/§6)
# ===========================================================================


class TestContent:
    async def _one(self, client: httpx.AsyncClient, key: str, msg_id: int) -> dict[str, Any]:
        resp = await client.get(_URL, headers={"X-API-Key": key}, params={"limit": 200})
        assert resp.status_code == 200, resp.text
        messages: list[dict[str, Any]] = resp.json()["messages"]
        for m in messages:
            if m["id"] == msg_id:
                return m
        raise AssertionError(f"message {msg_id} not in response")

    async def test_large_body_not_truncated(
        self,
        client: httpx.AsyncClient,
        api_key_on: str,
        owner: Any,
        make_mail_account: Callable[..., Any],
        make_message: Callable[..., Any],
    ) -> None:
        acc = await make_mail_account(owner.id, "big@example.com")
        big = "X" * 20_000  # > 16 KB
        m = await make_message(acc.id, uid=1, body_text=big)
        got = await self._one(client, api_key_on, m.id)
        assert got["body_text"] == big
        assert len(got["body_text"]) == 20_000

    async def test_body_is_raw_blank_line_runs_preserved(
        self,
        client: httpx.AsyncClient,
        api_key_on: str,
        owner: Any,
        make_mail_account: Callable[..., Any],
        make_message: Callable[..., Any],
    ) -> None:
        """External body is RAW — the stored body is returned verbatim, with no
        render-time normalisation applied. Runs of blank lines survive
        byte-for-byte. ADR-0029 §3/§7.

        TD-060: this case used to contrast the raw body against
        ``collapse_blank_lines_text`` (the UI MessageDetail view). That helper
        and the UI were removed with the Jinja/Telegram subsystems (ADR-0044);
        the surviving guarantee — verbatim passthrough — is asserted directly.
        """
        raw = "para one\n\n\n\n\npara two"  # 4 blank lines between paragraphs
        acc = await make_mail_account(owner.id, "blanks@example.com")
        m = await make_message(acc.id, uid=1, body_text=raw)
        got = await self._one(client, api_key_on, m.id)
        # External returns the verbatim stored body — blank-line run intact.
        assert got["body_text"] == raw
        assert "\n\n\n\n\n" in got["body_text"]

    async def test_body_present_false_empty_text_null_html(
        self,
        client: httpx.AsyncClient,
        api_key_on: str,
        owner: Any,
        make_mail_account: Callable[..., Any],
        make_message: Callable[..., Any],
    ) -> None:
        acc = await make_mail_account(owner.id, "nobody@example.com")
        m = await make_message(acc.id, uid=1, body_present=False, body_text="", body_html=None)
        got = await self._one(client, api_key_on, m.id)
        assert got["body_present"] is False
        assert got["body_text"] == ""
        assert got["body_html"] is None

    async def test_nullable_fields_serialized_as_null(
        self,
        client: httpx.AsyncClient,
        api_key_on: str,
        owner: Any,
        make_mail_account: Callable[..., Any],
        make_message: Callable[..., Any],
    ) -> None:
        acc = await make_mail_account(owner.id, "nulls@example.com", display_name=None)
        m = await make_message(
            acc.id,
            uid=1,
            subject=None,
            from_name=None,
            cc_addrs=None,
        )
        got = await self._one(client, api_key_on, m.id)
        assert got["subject"] is None
        assert got["from_name"] is None
        assert got["cc_addrs"] is None
        assert got["mail_account"]["display_name"] is None

    async def test_to_addrs_always_string(
        self,
        client: httpx.AsyncClient,
        api_key_on: str,
        owner: Any,
        make_mail_account: Callable[..., Any],
        make_message: Callable[..., Any],
    ) -> None:
        acc = await make_mail_account(owner.id, "toaddr@example.com")
        m = await make_message(acc.id, uid=1, to_addrs="")
        got = await self._one(client, api_key_on, m.id)
        assert isinstance(got["to_addrs"], str)
        assert got["to_addrs"] == ""

    async def test_mail_account_whitelist_no_secret_fields(
        self,
        client: httpx.AsyncClient,
        api_key_on: str,
        owner: Any,
        make_mail_account: Callable[..., Any],
        make_message: Callable[..., Any],
    ) -> None:
        """``mail_account`` exposes ONLY {id, email, display_name} — never
        passwords / oauth tokens / IMAP-UID / owner. ADR-0029 §2/§Security."""
        acc = await make_mail_account(owner.id, "secret@example.com", display_name="Public Name")
        m = await make_message(acc.id, uid=4242)
        got = await self._one(client, api_key_on, m.id)
        ma = got["mail_account"]
        assert set(ma.keys()) == {"id", "email", "display_name"}
        # No leakage of secret / internal columns anywhere in the DTO.
        forbidden = {
            "encrypted_password",
            "smtp_encrypted_password",
            "oauth_refresh_token_encrypted",
            "oauth_access_token_encrypted",
            "password",
            "user_id",
            "group_id",
            "imap_host",
            "imap_port",
        }
        assert forbidden.isdisjoint(set(ma.keys()))
        # ``uid`` (IMAP-UID) and ``mail_account_id`` are not top-level fields.
        assert "uid" not in got
        assert "mail_account_id" not in got

    async def test_internal_date_serialized(
        self,
        client: httpx.AsyncClient,
        api_key_on: str,
        owner: Any,
        make_mail_account: Callable[..., Any],
        make_message: Callable[..., Any],
    ) -> None:
        acc = await make_mail_account(owner.id, "date@example.com")
        m = await make_message(
            acc.id, uid=1, internal_date=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
        )
        got = await self._one(client, api_key_on, m.id)
        # ISO-8601 parseable back to the same instant.
        parsed = datetime.fromisoformat(got["internal_date"])
        assert parsed.astimezone(UTC) == datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


# ===========================================================================
# 5. CSRF-exempt (ADR-0029 §1)
# ===========================================================================


class TestCsrfExempt:
    async def test_get_external_not_rejected_by_csrf(
        self, client: httpx.AsyncClient, api_key_on: str
    ) -> None:
        # GET is a safe method anyway, but assert the prefix is exempt and the
        # response is the 200 auth-success (not a 403 csrf_failed).
        resp = await client.get(_URL, headers={"X-API-Key": api_key_on})
        assert resp.status_code == 200
        assert "error" not in resp.json()

    async def test_feature_off_returns_401_not_403_csrf(
        self, client: httpx.AsyncClient, set_external_api_key: Callable[[str], None]
    ) -> None:
        # Even with the feature off the failure is auth (401), never a CSRF 403
        # — the route is CSRF-exempt regardless.
        set_external_api_key("")
        resp = await client.get(_URL)
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] != "csrf_failed"
