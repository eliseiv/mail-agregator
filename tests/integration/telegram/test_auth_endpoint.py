"""Integration tests for ``POST /api/telegram/auth`` (ADR-0022 §1.2).

Covers:
- Linked path: valid initData + existing link → 200 ``linked=true`` +
  ``mas_session`` cookie set.
- Unlinked path: valid initData + no link → 200 ``linked=false`` +
  ``mas_tg_pending`` cookie + Redis token stored under ``tg_pending:{token}``.
- 401 ``init_data_expired`` for old auth_date.
- 401 ``invalid_init_data`` for tampered HMAC.
- 429 ``rate_limited`` per IP (31st request) and per telegram_user_id (11th).

Source of truth: ``backend/app/telegram/router.py`` +
``docs/04-api-contracts.md`` sec. 4a.
"""

from __future__ import annotations

import time
from typing import Any

import httpx
import pytest

from shared.redis_client import get_redis

from tests.integration.telegram.conftest import make_init_data

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Linked / unlinked
# ---------------------------------------------------------------------------


class TestLinkedPath:
    async def test_valid_init_data_with_link_returns_session_cookie(
        self,
        client: httpx.AsyncClient,
        super_admin_user: Any,
        make_link: Any,
    ) -> None:
        # Pre-link this Telegram user to the super-admin.
        tg_id = 50001
        await make_link(tg_id, super_admin_user.id)

        raw = make_init_data(telegram_user_id=tg_id)
        resp = await client.post("/api/telegram/auth", json={"init_data": raw})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body == {"linked": True, "redirect": "/"}
        # Session cookies set.
        assert resp.cookies.get("mas_session") is not None
        assert resp.cookies.get("mas_csrf") is not None
        # No pending cookie on the linked path.
        assert resp.cookies.get("mas_tg_pending") is None

    async def test_linked_path_dead_link_falls_through_to_unlinked(
        self,
        client: httpx.AsyncClient,
        super_admin_user: Any,
        make_link: Any,
    ) -> None:
        """A dead-marked link is treated as no link → unlinked branch."""
        tg_id = 50002
        await make_link(tg_id, super_admin_user.id, dead=True)

        raw = make_init_data(telegram_user_id=tg_id)
        resp = await client.post("/api/telegram/auth", json={"init_data": raw})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["linked"] is False
        assert body["redirect"] == "/login"
        # Pending cookie is set instead.
        assert resp.cookies.get("mas_tg_pending") is not None


class TestUnlinkedPath:
    async def test_valid_init_data_no_link_sets_pending_cookie_and_redis(
        self,
        client: httpx.AsyncClient,
    ) -> None:
        tg_id = 60001
        raw = make_init_data(telegram_user_id=tg_id)
        resp = await client.post("/api/telegram/auth", json={"init_data": raw})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body == {"linked": False, "redirect": "/login"}
        pending = resp.cookies.get("mas_tg_pending")
        assert pending is not None
        # No session cookies on the unlinked path.
        assert resp.cookies.get("mas_session") is None

        # Redis must hold the token → tg_user_id mapping.
        r = get_redis()
        stored = await r.get(f"tg_pending:{pending}")
        assert stored is not None
        # redis may return bytes or str depending on encoding.
        if isinstance(stored, bytes):
            stored = stored.decode()
        assert int(stored) == tg_id


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------


class TestFailureModes:
    async def test_expired_init_data_returns_401_init_data_expired(
        self, client: httpx.AsyncClient
    ) -> None:
        # auth_date older than the TTL (5 min).
        old = int(time.time()) - 600
        raw = make_init_data(telegram_user_id=70001, auth_date=old)
        resp = await client.post("/api/telegram/auth", json={"init_data": raw})
        assert resp.status_code == 401, resp.text
        body = resp.json()
        assert body["error"]["code"] == "init_data_expired"

    async def test_tampered_hash_returns_401_invalid_init_data(
        self, client: httpx.AsyncClient
    ) -> None:
        raw = make_init_data(telegram_user_id=70002, tamper_hash=True)
        resp = await client.post("/api/telegram/auth", json={"init_data": raw})
        assert resp.status_code == 401, resp.text
        body = resp.json()
        assert body["error"]["code"] == "invalid_init_data"

    async def test_bad_json_body_returns_400_validation_error(
        self, client: httpx.AsyncClient
    ) -> None:
        resp = await client.post(
            "/api/telegram/auth",
            content=b"not json at all",
            headers={"Content-Type": "application/json"},
        )
        # Either 400 validation_error or 422 — our pipeline maps to 400.
        assert resp.status_code in (400, 422), resp.text

    async def test_missing_init_data_field_returns_400(
        self, client: httpx.AsyncClient
    ) -> None:
        resp = await client.post("/api/telegram/auth", json={"wrong_key": "x"})
        assert resp.status_code in (400, 422), resp.text


# ---------------------------------------------------------------------------
# Rate limits
# ---------------------------------------------------------------------------


class TestRateLimits:
    async def test_rate_limit_per_ip_after_30_requests(
        self, client: httpx.AsyncClient
    ) -> None:
        # ``LIMIT_TG_AUTH_IP`` is 30 / min. The 31st request from the same
        # IP must come back as 429.
        statuses: list[int] = []
        # Send slightly tampered initData so we don't blow the per-tg limit
        # before exhausting the IP limit. Each call has a distinct tg_user_id
        # to be safe.
        for i in range(35):
            raw = make_init_data(
                telegram_user_id=80000 + i,
                tamper_hash=True,  # cheap path: HMAC fail keeps state minimal
            )
            r = await client.post("/api/telegram/auth", json={"init_data": raw})
            statuses.append(r.status_code)
            if r.status_code == 429:
                break
        assert 429 in statuses, f"never hit 429 — statuses: {statuses}"

    async def test_rate_limit_per_tg_user_after_10_valid_calls(
        self, client: httpx.AsyncClient
    ) -> None:
        """The 11th VALID request with the same telegram_user_id must 429.

        The per-tg limit fires only AFTER HMAC succeeds, so we must keep
        tg_user_id constant and the HMAC valid for each call.
        """
        tg_id = 80100
        statuses: list[int] = []
        for _ in range(15):
            raw = make_init_data(telegram_user_id=tg_id)
            r = await client.post("/api/telegram/auth", json={"init_data": raw})
            statuses.append(r.status_code)
            if r.status_code == 429:
                break
        assert 429 in statuses, f"never hit 429 — statuses: {statuses}"

    async def test_429_includes_retry_after(
        self, client: httpx.AsyncClient
    ) -> None:
        # Exhaust the IP limit then check headers on the 429 response.
        for i in range(35):
            raw = make_init_data(telegram_user_id=80200 + i, tamper_hash=True)
            r = await client.post("/api/telegram/auth", json={"init_data": raw})
            if r.status_code == 429:
                assert int(r.headers["retry-after"]) > 0
                return
        pytest.fail("never hit 429")
