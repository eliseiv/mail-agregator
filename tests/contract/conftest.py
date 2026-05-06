"""Re-use integration fixtures (truncate DB, flush Redis, clean MinIO,
``app``/``client``).

Contract tests speak HTTP through the same ASGI app as integration tests.
"""

from __future__ import annotations

from tests.integration.conftest import (  # noqa: F401
    _db_truncate_all,
    _minio_clean,
    _redis_flush,
    app,
    client,
)
