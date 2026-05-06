"""Integration tests for the imperative rate limiter.

Source of truth: ``backend/app/rate_limit.py`` + ADR-0009 +
``docs/04-api-contracts.md`` sec.8.
"""

from __future__ import annotations

import httpx
import pytest

from shared.config import get_settings

pytestmark = pytest.mark.integration


class TestLoginRateLimit:
    async def test_excessive_login_returns_429_or_lockout(self, client: httpx.AsyncClient) -> None:
        get_settings()
        # Capacity is 5 / 15 min on (username + IP). The 6th attempt within
        # the window must be rejected. Either with 423 (account already
        # locked, server-side prefers that) or 429 (rate-limit) — both are
        # acceptable per ADR-0009.
        username = "rate_limit_test_user_zzz"
        statuses: list[int] = []
        for i in range(7):
            r = await client.post(
                "/login",
                data={"username": username, "password": f"p{i}"},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            statuses.append(r.status_code)
        # The trailing attempts must include at least one 429.
        assert 429 in statuses, f"never hit 429 in: {statuses}"

    async def test_429_includes_retry_after(self, client: httpx.AsyncClient) -> None:
        username = "rate_limit_retry_after_user"
        for _ in range(7):
            await client.post(
                "/login",
                data={"username": username, "password": "x"},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        r = await client.post(
            "/login",
            data={"username": username, "password": "x"},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert r.status_code == 429
        assert int(r.headers["retry-after"]) > 0
