"""Fixtures for the ADR-0048 generic-send suite (phase A2.1).

These tests exercise the real endpoint / DB / MIME pipeline and therefore need the
full stack (postgres + redis). They live under ``tests/unit`` **on purpose**:
CI only gates ``tests/unit | tests/worker | tests/frontend``
(``.github/workflows/ci.yml``), so the coverage of the endpoint that the CRM calls
to answer a message must sit here to actually block a regression — the endpoint's
absence was a live production bug (TD-059), and a non-gated suite would not have
caught it.

We re-export the live-app fixtures from ``tests.integration.conftest`` rather than
duplicate them (same pattern the ADR-0038 suite used). Importing them into THIS
conftest scopes the autouse isolation fixtures (``_db_truncate_all`` /
``_redis_flush``) to this subdirectory only — the pure
``tests/unit`` files stay infra-free.
"""

from __future__ import annotations

from tests.integration.conftest import (  # noqa: F401  (fixture re-export)
    _db_truncate_all,
    _redis_flush,
    app,
    client,
)
