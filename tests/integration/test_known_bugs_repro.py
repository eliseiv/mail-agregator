"""Regression tests for the two production bugs found by QA.

Originally these were xfailed (strict=True) so the suite would go green only
when the bugs were fixed. After the round-3 backend fix both started reporting
XPASS — i.e. the bugs are closed. The xfail markers were removed and the
tests now act as positive regressions: any future revert would surface as a
hard failure.

BUG-001 (closed): Routes that combine ``user: CurrentUser`` (which calls
``UsersRepo.get_by_id`` → SQLAlchemy autobegins a read transaction) with
``async with db.begin():`` previously raised ``InvalidRequestError: A
transaction is already begun on this Session``. Affected every state-changing
endpoint: ``POST/PATCH/DELETE /api/mail-accounts/*``, ``POST /api/messages/*``,
``POST/PATCH/DELETE /api/admin/users/*``, ``POST /api/messages/send``.

  Fix: ``backend/app/deps.py::current_user`` now ``await db.commit()`` after
  the SELECT (and ``await db.rollback()`` on the user-vanished branch),
  closing the autobegun read-tx so handlers can open their own write tx.

BUG-002 (closed): ``CSRFError`` raised inside ``CSRFMiddleware.dispatch`` was
NOT caught by the FastAPI exception handlers installed via
``install_exception_handlers``. The exception escaped the ASGI stack and
clients saw a 500 / connection error instead of the documented 403.

  Fix: ``CSRFMiddleware.dispatch`` now wraps the verification in a
  try/except CSRFError and returns the same JSON envelope as the central
  ``_domain_handler`` directly.
"""

from __future__ import annotations

import httpx
import pytest

from shared.config import get_settings

pytestmark = pytest.mark.integration


async def test_bug001_create_user_endpoint_returns_201(
    client: httpx.AsyncClient,
) -> None:
    """``POST /api/admin/users`` succeeds (201) instead of 500ing on the
    autobegun-tx collision."""
    s = get_settings()
    login = await client.post(
        "/login",
        data={"username": s.ADMIN_LOGIN, "password": s.ADMIN_PASSWORD},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    csrf = login.cookies["mas_csrf"]
    resp = await client.post(
        "/api/admin/users",
        json={"username": "bug001_user", "email": None},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 201, (
        f"BUG-001 regression: got {resp.status_code} body={resp.text[:200]}"
    )
    body = resp.json()
    assert body["username"] == "bug001_user"


async def test_bug002_missing_csrf_returns_403(
    client: httpx.AsyncClient,
) -> None:
    """``POST`` without CSRF token returns the documented 403 envelope
    instead of bubbling the ``CSRFError`` out of the ASGI stack."""
    s = get_settings()
    await client.post(
        "/login",
        data={"username": s.ADMIN_LOGIN, "password": s.ADMIN_PASSWORD},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    # Hit a state-changing endpoint without any CSRF token.
    resp = await client.post(
        "/api/admin/users",
        json={"username": "bug002", "email": None},
        # No X-CSRF-Token header, no csrf_token in body.
    )
    assert resp.status_code == 403, (
        f"BUG-002 regression: got {resp.status_code}"
    )
    body = resp.json()
    assert body["error"]["code"] == "csrf_failed"
