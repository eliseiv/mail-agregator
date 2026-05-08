"""Jinja2Templates singleton + helpers exposed to templates.

The ``templates/`` folder is owned by the frontend agent — backend only
provides the renderer and helpers (``csrf_input``, ``flash_messages``).

Use :func:`render` instead of ``templates.TemplateResponse`` directly when
the page is allowed to surface flash messages (per ADR-0015 it should
read-and-clear ``flash:{session_id}`` and pass them to the template as
``flashes``).
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape
from starlette.requests import Request
from starlette.responses import Response

from backend.app.flash import consume_flashes

_TEMPLATES_DIR = Path(__file__).parent / "templates"

templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

# Cache-busting token for static assets — refreshes on every process start so
# we don't ship stale JS/CSS to clients that aggressively cache by URL.
_STATIC_VERSION = str(int(time.time()))


# --- Globals for templates --------------------------------------------------


def _csrf_input(csrf_token: str) -> Markup:
    """Render ``<input type="hidden" name="csrf_token" value="...">``."""
    return Markup(f'<input type="hidden" name="csrf_token" value="{escape(csrf_token)}">')


def _flash_messages(*flashes: dict[str, Any]) -> Markup:
    """Render flash messages. The frontend can override the markup via CSS;
    we just emit a semantic block.

    Source-of-truth keys per ``docs/05-modules.md`` sec. 3 + ADR-0015 are
    ``category`` and ``text`` (matches what :func:`backend.app.flash.flash`
    writes and :func:`backend.app.flash.consume_flashes` returns).
    """
    if not flashes:
        return Markup("")
    parts = []
    for f in flashes:
        category = escape(str(f.get("category", "info")))
        text = escape(str(f.get("text", "")))
        parts.append(f'<div class="flash flash-{category}">{text}</div>')
    return Markup("\n".join(parts))


def _format_bytes(n: int | None) -> str:
    if n is None or n < 0:
        return "0 B"
    n_f = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n_f < 1024:
            return f"{n_f:.1f} {unit}" if unit != "B" else f"{int(n_f)} {unit}"
        n_f /= 1024
    return f"{n_f:.1f} PiB"


# Register globals + filters on the underlying Jinja env.
templates.env.globals["csrf_input"] = _csrf_input
templates.env.globals["flash_messages"] = _flash_messages
templates.env.globals["static_v"] = _STATIC_VERSION
templates.env.filters["format_bytes"] = _format_bytes
templates.env.autoescape = True


# --- Render helper (ADR-0015 — flash injection) -----------------------------


async def render(
    request: Request,
    name: str,
    context: dict[str, Any] | None = None,
    *,
    status_code: int = 200,
) -> Response:
    """Render an HTML template with flash messages injected automatically.

    Always consumes flash messages from Redis (read-and-clear, see
    :mod:`backend.app.flash`) and merges them into the template context as
    ``flashes`` — the value can be empty.

    If the caller already supplies ``flashes`` in ``context`` (for example,
    a re-render of a form right after writing a flash within the same
    request), those entries are preserved and *prepended* — but flashes
    already in Redis from a previous request are still consumed.
    """
    base: dict[str, Any] = dict(context or {})
    redis_flashes = await consume_flashes(request)
    inline_flashes = base.pop("flashes", None) or []
    base["flashes"] = list(inline_flashes) + redis_flashes
    return templates.TemplateResponse(
        request,
        name,
        base,
        status_code=status_code,
    )
