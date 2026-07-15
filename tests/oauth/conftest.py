"""Fixtures for the Outlook OAuth2 test package (Sprint B / ADR-0025).

These tests need:

* The integration autouse fixtures (truncate DB, flush Redis) so
  each test starts from a clean state — re-exported from the integration
  package conftest (same approach as ``tests/worker/conftest.py``).
* A way to flip ``OUTLOOK_OAUTH_ENABLED`` on/off. The flag is *derived* from
  ``OUTLOOK_CLIENT_ID`` + ``OUTLOOK_CLIENT_SECRET`` on the cached
  :class:`shared.config.Settings` singleton (``get_settings()`` is
  ``lru_cache``-d). To toggle it for a test we set the two env vars and clear
  the cache *before* the app/service reads settings, then restore afterwards.
* A mock Microsoft token endpoint via :class:`httpx.MockTransport` so no real
  Azure App / network is required (ADR-0025 Q-OAUTH-3 / TD-031). The service
  accepts an injected ``http_client`` exactly for this.

No real Azure credentials exist in this environment; every token-endpoint
interaction is mocked.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport

from shared.config import get_settings

# Re-export the integration autouse fixtures so this package gets the same
# clean-state guarantees (DB truncate / Redis flush).
from tests.integration.conftest import (  # noqa: F401
    _db_truncate_all,
    _redis_flush,
    login_as_admin,
    two_step_login,
)

# Mock Azure App credentials — *not* secrets, only used to flip the feature
# flag and to assemble the authorize URL. The token endpoint is mocked, so
# these never reach Microsoft.
MOCK_CLIENT_ID = "test-client-id-0001"
MOCK_CLIENT_SECRET = "test-client-secret-0001"  # fake, mock-only
MOCK_REDIRECT_URI = "http://test/api/oauth/outlook/callback"


def _reset_settings_cache() -> None:
    get_settings.cache_clear()


@pytest.fixture
def enable_outlook_oauth(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Set the Azure App env vars so ``outlook_oauth_enabled`` flips True.

    Clears the ``get_settings`` lru_cache before *and* after so neither this
    test nor the next sees a stale singleton.
    """
    monkeypatch.setenv("OUTLOOK_CLIENT_ID", MOCK_CLIENT_ID)
    monkeypatch.setenv("OUTLOOK_CLIENT_SECRET", MOCK_CLIENT_SECRET)
    monkeypatch.setenv("OUTLOOK_REDIRECT_URI", MOCK_REDIRECT_URI)
    monkeypatch.setenv("OUTLOOK_TENANT", "consumers")
    _reset_settings_cache()
    yield
    _reset_settings_cache()


@pytest.fixture
def disable_outlook_oauth(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Force ``outlook_oauth_enabled`` False (no client id/secret)."""
    monkeypatch.setenv("OUTLOOK_CLIENT_ID", "")
    monkeypatch.setenv("OUTLOOK_CLIENT_SECRET", "")
    _reset_settings_cache()
    yield
    _reset_settings_cache()


# ---------------------------------------------------------------------------
# App + client that observe whatever OUTLOOK_* env the test set up.
# These intentionally do NOT depend on the integration ``app`` fixture so the
# settings cache is cleared *before* the app reads it.
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def oauth_app() -> AsyncIterator[Any]:
    """FastAPI app built after the OUTLOOK_* env was (maybe) set."""
    from tests.conftest import _pg_available, _redis_available

    if not (_pg_available() and _redis_available()):
        pytest.skip("integration deps missing")
    from shared.db import dispose_engine

    await dispose_engine()
    from backend.app.main import create_app

    application = create_app()
    async with application.router.lifespan_context(application):
        yield application


@pytest_asyncio.fixture
async def oauth_client(oauth_app: Any) -> AsyncIterator[httpx.AsyncClient]:
    transport = ASGITransport(app=oauth_app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        follow_redirects=False,
    ) as c:
        yield c
