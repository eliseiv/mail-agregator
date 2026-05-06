"""Render every Jinja2 template with a typical context. The point is to
catch missing-attr, undefined-name, or syntax issues before they hit the
browser.

Source of truth: ``backend/app/templates/`` directory + ``templates.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from jinja2 import ChainableUndefined, Environment, FileSystemLoader

from backend.app.templates import _csrf_input, _flash_messages, _format_bytes

pytestmark = pytest.mark.frontend

# Templates dir
import pathlib

TEMPLATES = pathlib.Path(
    pathlib.Path(__file__).parent.parent.parent
    / "backend"
    / "app"
    / "templates"
)


def _env() -> Environment:
    """Build a stand-alone Jinja env identical to the production one
    so we can render templates without booting the full app."""
    # Use ChainableUndefined (the production templates module uses Jinja's
    # default ``Undefined`` via ``Jinja2Templates``); chained access on a
    # missing var doesn't blow up, mirroring real-world template behaviour.
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES)),
        autoescape=True,
        undefined=ChainableUndefined,
    )
    env.globals["csrf_input"] = _csrf_input
    env.globals["flash_messages"] = _flash_messages
    env.filters["format_bytes"] = _format_bytes
    # Templates often pull `request.url_for` / `url_for` etc.
    env.globals["url_for"] = lambda name, **_: f"/{name}"
    env.globals["request"] = SimpleNamespace(
        url=SimpleNamespace(path="/"),
        scope={"path": "/"},
    )
    return env


def _ctx(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    base: dict[str, Any] = {
        "csrf_token": "dummy_token",
        "session": SimpleNamespace(role="admin", user_id=1),
        "flashes": [],
    }
    if extra:
        base.update(extra)
    return base


class TestTemplatesRender:
    def test_login(self) -> None:
        out = _env().get_template("login.html").render(_ctx({"flash": None}))
        assert "<form" in out

    def test_set_password(self) -> None:
        out = _env().get_template("set_password.html").render(
            _ctx({"username": "alice", "flash": None})
        )
        assert "<form" in out

    def test_inbox(self) -> None:
        out = _env().get_template("inbox.html").render(
            _ctx(
                {
                    "items": [],
                    "next_cursor": None,
                    "accounts": [],
                    "selected_account_id": None,
                    "unread_only": False,
                    "unread_count": 0,
                }
            )
        )
        assert "<" in out

    def test_compose(self) -> None:
        out = _env().get_template("compose.html").render(
            _ctx(
                {
                    "accounts": [],
                    "form": {"to": "", "cc": "", "bcc": "", "subject": "", "body": ""},
                    "default_from_account_id": None,
                    "reply_to": None,
                    "error_message": None,
                }
            )
        )
        assert "<form" in out

    def test_message_view(self) -> None:
        msg = SimpleNamespace(
            id=1,
            from_addr="x@y.com",
            from_name=None,
            to_addrs="me@y.com",
            cc_addrs=None,
            subject="hi",
            internal_date=datetime.now(UTC),
            body_text="hi",
            body_truncated=False,
            body_present=True,
            in_reply_to=None,
            is_read=False,
            mail_account_id=1,
            mail_account_email="me@y.com",
            attachments=[],
        )
        out = _env().get_template("message_view.html").render(_ctx({"message": msg}))
        assert "hi" in out

    def test_accounts_list(self) -> None:
        out = _env().get_template("accounts/list.html").render(
            _ctx({"accounts": []})
        )
        assert "<" in out

    def test_accounts_form_create(self) -> None:
        out = _env().get_template("accounts/form.html").render(
            _ctx({"account": None})
        )
        assert "<form" in out

    def test_admin_users(self) -> None:
        out = _env().get_template("admin/users.html").render(
            _ctx(
                {
                    "users": [],
                    "total": 0,
                    "page": 1,
                    "limit": 50,
                    "q": "",
                    "current_admin_id": 1,
                }
            )
        )
        assert "<" in out

    def test_admin_audit(self) -> None:
        out = _env().get_template("admin/audit.html").render(
            _ctx(
                {
                    "items": [],
                    "total": 0,
                    "page": 1,
                    "limit": 50,
                    "action_filter": "",
                }
            )
        )
        assert "<" in out

    @pytest.mark.parametrize("name", ["403.html", "404.html", "4xx.html", "500.html", "5xx.html"])
    def test_error_pages(self, name: str) -> None:
        out = _env().get_template(f"errors/{name}").render(
            _ctx({"detail": None, "request_id": "rid"})
        )
        assert "<" in out
