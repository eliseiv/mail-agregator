"""Fixtures for the front-end DECOMMISSION regression suite (ADR-0044 §5).

Nothing renders here any more — the package now proves the HTML surface is GONE
(404 on every UI URL) while ``/healthz`` / ``/readyz`` and the external API stay
up. That needs the live ASGI app, so we re-export the integration fixtures
(``app`` / ``client`` + the autouse DB/Redis isolation) exactly as
``tests/worker/conftest.py`` does. ``tests/frontend`` is part of the CI test scope
(``.github/workflows/ci.yml``), so this regression actually gates a re-mounting of
the UI.
"""

from __future__ import annotations

from tests.integration.conftest import (  # noqa: F401  (fixture re-export)
    _db_truncate_all,
    _redis_flush,
    app,
    client,
)
