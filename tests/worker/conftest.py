"""Re-use integration fixtures (truncate DB, flush Redis, clean MinIO).

Worker tests need the same DB/Redis/MinIO state as integration tests, so
we just import the autouse fixtures.
"""

from __future__ import annotations

# Re-export autouse fixtures from the integration package so pytest picks
# them up for tests/worker/ too.
from tests.integration.conftest import (  # noqa: F401
    _db_truncate_all,
    _minio_clean,
    _redis_flush,
)
