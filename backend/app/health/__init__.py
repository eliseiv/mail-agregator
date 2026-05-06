"""Health endpoints (``/healthz``, ``/readyz``) and ``/api/me``."""

from backend.app.health.router import router

__all__ = ["router"]
