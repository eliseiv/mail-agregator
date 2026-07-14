"""Regression suite for the FRONT-END DECOMMISSION (ADR-0044 §5, phase A1/A3).

The Jinja UI, the static assets, the cookie sessions, CSRF and the method override
are gone; the aggregator is a machine-only connector. This package used to render
templates — the template tests were deleted with the templates. What replaces them
is the *acceptance criterion of §5*, asserted as a test:

    "after the deploy, opening ANY html URL of the aggregator (``/``, ``/login``,
     ``/accounts``, ``/messages``, ``/tags``, ``/admin``, ``/static/*``) → **404**;
     ``/healthz`` / ``/readyz`` → 200; the external API works under the key."

Two layers:

1. **Import layer** (no infra, always runs): ``backend.app.templates`` must not be
   importable at all. The module ``backend/app/templates.py`` AND the package
   directory ``backend/app/templates/`` were both removed — had only the ``.py``
   gone, the leftover directory would still resolve as an implicit *namespace
   package* (a phantom ``backend.app.templates`` that imports fine and exports
   nothing), which is exactly the failure mode this test pins. Same for the other
   cookie-UI modules (``flash`` / ``cookies`` / ``sessions`` / ``csrf``).
2. **HTTP layer** (needs the stack): every dead HTML route answers 404 through the
   REAL ASGI app, while ``/healthz`` / ``/readyz`` still answer 200 and ``/readyz``
   carries NO S3 leg (MinIO left the readiness contract with attachments).

Source of truth: ``docs/adr/ADR-0044-decommission-runbook.md`` §5 +
``backend/app/main.py::create_app`` + ``backend/app/health/router.py``.
"""

from __future__ import annotations

import importlib

import httpx
import pytest

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# 1. Import layer — the UI modules are GONE (no phantom namespace package)
# ---------------------------------------------------------------------------


class TestDeadModulesAreUnimportable:
    @pytest.mark.parametrize(
        "module",
        [
            "backend.app.templates",  # Jinja env + the templates/ dir
            "backend.app.flash",
            "backend.app.cookies",
            "backend.app.sessions",
            "backend.app.csrf",
            "backend.app.admin.router",
            "backend.app.auth.router",
            "backend.app.accounts.router",
            "backend.app.messages.service",
            "backend.app.tags.service",
            "backend.app.telegram.bot",
            "backend.app.webhooks.dispatch_service",
            "backend.app.forwarding.service",
            "backend.app.groups.service",
            "backend.app.send.router",
            "backend.app.oauth.router",
        ],
    )
    def test_module_is_gone(self, module: str) -> None:
        # ``ModuleNotFoundError`` (a subclass of ImportError) — NOT an ImportError
        # for a missing *name*: the module itself must not exist. An implicit
        # namespace package left behind by an emptied directory would import
        # silently and fail this assertion.
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(module)

    def test_app_graph_imports_clean(self) -> None:
        # ADR-0044 §9.1: the whole ``create_app()`` graph must import without a
        # dangling reference to any removed symbol.
        importlib.import_module("backend.app.main")

    def test_worker_graph_imports_clean(self) -> None:
        # ADR-0044 §9.2: same for the worker entrypoint.
        importlib.import_module("worker.app.main")


# ---------------------------------------------------------------------------
# 2. HTTP layer — every HTML URL is a 404; health stays up
# ---------------------------------------------------------------------------

_DEAD_HTML_URLS = [
    "/",
    "/login",
    "/login/password",
    "/logout",
    "/accounts",
    "/accounts/new",
    "/messages",
    "/compose",
    "/tags",
    "/admin",
    "/admin/users",
    "/admin/audit",
    "/groups",
    "/integrations",
    "/forwarding",
    "/static/css/main.css",
    "/static/js/inbox.js",
]


class TestHtmlSurfaceIsGone:
    @pytest.mark.parametrize("url", _DEAD_HTML_URLS)
    async def test_html_url_404(self, client: httpx.AsyncClient, url: str) -> None:
        resp = await client.get(url)
        # 404 — not a 30x to /login (the friendly redirect handler is gone too,
        # ADR-0044 §5) and not a 405.
        assert resp.status_code == 404, f"{url} -> {resp.status_code}"

    async def test_no_static_mount(self, client: httpx.AsyncClient) -> None:
        # ``app.mount("/static", StaticFiles(...))`` is removed: a 404 from the
        # router, not a StaticFiles "directory not found" 500.
        resp = await client.get("/static/anything.css")
        assert resp.status_code == 404

    async def test_login_post_is_gone_too(self, client: httpx.AsyncClient) -> None:
        # The cookie-session login endpoint (POST) went with the UI.
        resp = await client.post("/login", data={"username": "admin"})
        assert resp.status_code == 404


class TestHealthSurvives:
    async def test_healthz_200(self, client: httpx.AsyncClient) -> None:
        resp = await client.get("/healthz")
        assert resp.status_code == 200

    async def test_readyz_200_without_s3_leg(self, client: httpx.AsyncClient) -> None:
        resp = await client.get("/readyz")
        assert resp.status_code == 200
        body = resp.json()
        # MinIO/S3 left the readiness contract with the attachments (ADR-0043 §4 /
        # ADR-0044 §5): the probe must not report — nor depend on — an S3 leg.
        flat = str(body).lower()
        assert "s3" not in flat
        assert "minio" not in flat
        assert "storage" not in flat


class TestExternalApiStillMounted:
    async def test_external_pull_requires_key_not_404(self, client: httpx.AsyncClient) -> None:
        # The machine surface is the ONLY thing left mounted: an unauthenticated
        # call must be 401 (route exists), never 404 (route gone).
        resp = await client.get("/api/external/messages")
        assert resp.status_code == 401

    async def test_external_send_route_exists(self, client: httpx.AsyncClient) -> None:
        # ADR-0048 §1 / phase A2.1 — the route the CRM calls. Unauthenticated →
        # 401, which proves the route is mounted (a missing route answers 404 —
        # that WAS the production bug, TD-059).
        resp = await client.post("/api/external/mailboxes/1/send", json={})
        assert resp.status_code == 401
