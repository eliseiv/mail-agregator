"""Fixtures for the ADR-0038 DB-backed suites.

These tests exercise real endpoints / DB / crypto and therefore need the full
stack (postgres + redis + minio). They live under ``tests/unit`` on purpose:
CI only gates ``tests/unit | tests/worker | tests/frontend`` (see project
memory ``ci-test-selection``), so the ADR-0038 coverage must sit here to
actually block a regression.

We re-export the live-app fixtures from ``tests.integration.conftest`` rather
than duplicate them. Importing them into this conftest scopes the autouse
isolation fixtures (``_db_truncate_all`` / ``_redis_flush`` / ``_minio_clean``)
to *this* subdirectory only — the pure ``tests/unit`` files are untouched and
keep running without docker.
"""

from __future__ import annotations

from tests.integration.conftest import (  # noqa: F401  (fixture re-export)
    _db_truncate_all,
    _minio_clean,
    _redis_flush,
    app,
    client,
    login_as,
)
