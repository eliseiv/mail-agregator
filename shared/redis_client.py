"""Single async Redis client used by api and worker.

Note: file is named ``redis_client.py``, not ``redis.py``, to avoid shadowing
the third-party ``redis`` package on import.
"""

from __future__ import annotations

import redis.asyncio as redis_asyncio

from shared.config import get_settings

_redis: redis_asyncio.Redis | None = None


def get_redis() -> redis_asyncio.Redis:
    """Return a process-wide async Redis client."""
    global _redis
    if _redis is None:
        settings = get_settings()
        _redis = redis_asyncio.from_url(
            settings.REDIS_URL,
            encoding="utf-8",
            decode_responses=True,
            health_check_interval=30,
        )
    return _redis


async def close_redis() -> None:
    """Close the global client. Used by shutdown hooks and tests."""
    global _redis
    if _redis is not None:
        # ``aclose`` is preferred in redis>=5; ``close`` exists in older
        # versions. Both are safe to call.
        close_method = getattr(_redis, "aclose", None) or _redis.close
        await close_method()
        _redis = None
