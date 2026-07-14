"""Health endpoints (``/healthz``, ``/readyz``)."""

from backend.app.health.router import router

__all__ = ["router"]
