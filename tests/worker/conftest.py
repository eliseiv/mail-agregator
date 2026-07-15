"""Re-use integration fixtures (truncate DB, flush Redis, app/client).

Worker tests need the same DB/Redis state as integration tests, so
we just import the autouse fixtures. ``app`` / ``client`` are re-exported as
well: the ADR-0046 status-hook suite drives the backend HTTP surface (H5/H6 fire
in the routers) and lives here because ``tests/worker`` — unlike ``tests/unit`` —
carries the DB/Redis cleanup fixtures and is inside the CI test scope.
"""

from __future__ import annotations

# Re-export fixtures from the integration package so pytest picks them up for
# tests/worker/ too (``_db_truncate_all`` / ``_redis_flush`` are autouse).
from tests.integration.conftest import (  # noqa: F401
    _db_truncate_all,
    _redis_flush,
    app,
    client,
)
