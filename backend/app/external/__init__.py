"""External PULL-API package (ADR-0029).

``GET /api/external/messages`` — a B2B partner incrementally pulls ALL system
messages with a keyset cursor over ``messages.id``. Auth is a static
``EXTERNAL_API_KEY`` (``X-API-Key`` or ``Authorization: Bearer``); no cookie
session, CSRF-exempt, read-only, super_admin visibility (canonical-deduped).
"""

from __future__ import annotations

from backend.app.external.router import router

__all__ = ["router"]
