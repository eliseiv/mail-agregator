"""Verify rendered HTML complies with the CSP enforced by
:class:`SecurityHeadersMiddleware` (default-src 'self'; style-src 'self';
script-src 'self'; ...).

That CSP forbids:
- inline ``<script>`` blocks (only ``<script src="..."></script>`` allowed)
- inline ``<style>`` blocks
- inline ``style="..."`` attributes
- inline event handlers (``onclick="..."`` etc.)

Source of truth: ``backend/app/middlewares.py::SecurityHeadersMiddleware._CSP``.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from jinja2 import ChainableUndefined, Environment, FileSystemLoader

from backend.app.templates import _csrf_input, _flash_messages, _format_bytes

pytestmark = pytest.mark.frontend

import pathlib

TEMPLATES = pathlib.Path(
    pathlib.Path(__file__).parent.parent.parent / "backend" / "app" / "templates"
)


def _env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES)),
        autoescape=True,
        undefined=ChainableUndefined,
    )
    env.globals["csrf_input"] = _csrf_input
    env.globals["flash_messages"] = _flash_messages
    env.filters["format_bytes"] = _format_bytes
    env.globals["url_for"] = lambda name, **_: f"/{name}"
    env.globals["request"] = SimpleNamespace(url=SimpleNamespace(path="/"), scope={"path": "/"})
    return env


def _ctx(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    base: dict[str, Any] = {
        "csrf_token": "tok",
        "session": SimpleNamespace(role="admin", user_id=1),
        "flashes": [],
    }
    if extra:
        base.update(extra)
    return base


# Inline-content / inline-event regexes. We deliberately permit
# ``<script src="...">`` and ``<link rel="stylesheet">`` (external assets).
_INLINE_SCRIPT_RE = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>", re.IGNORECASE)
_INLINE_STYLE_BLOCK_RE = re.compile(r"<style[^>]*>", re.IGNORECASE)
_INLINE_STYLE_ATTR_RE = re.compile(r'\sstyle\s*=\s*"', re.IGNORECASE)
_INLINE_EVENT_RE = re.compile(r'\son[a-z]+\s*=\s*"', re.IGNORECASE)


def _render(name: str, ctx: dict[str, Any]) -> str:
    return _env().get_template(name).render(ctx)


_TEMPLATE_CASES: list[tuple[str, dict[str, Any]]] = [
    ("login.html", _ctx({"flash": None})),
    ("set_password.html", _ctx({"username": "alice", "flash": None})),
    (
        "inbox.html",
        _ctx(
            {
                "items": [],
                "next_cursor": None,
                "accounts": [],
                "selected_account_id": None,
                "unread_only": False,
                "unread_count": 0,
            }
        ),
    ),
    (
        "compose.html",
        _ctx(
            {
                "accounts": [],
                "form": {
                    "to": "",
                    "cc": "",
                    "bcc": "",
                    "subject": "",
                    "body": "",
                },
                "default_from_account_id": None,
                "reply_to": None,
                "error_message": None,
            }
        ),
    ),
    (
        "accounts/list.html",
        _ctx({"accounts": []}),
    ),
    (
        "accounts/form.html",
        _ctx({"account": None}),
    ),
    (
        "admin/users.html",
        _ctx(
            {
                "users": [],
                "total": 0,
                "page": 1,
                "limit": 50,
                "q": "",
                "current_admin_id": 1,
            }
        ),
    ),
    (
        "admin/audit.html",
        _ctx(
            {
                "items": [],
                "total": 0,
                "page": 1,
                "limit": 50,
                "action_filter": "",
            }
        ),
    ),
]


@pytest.mark.parametrize("name,ctx", _TEMPLATE_CASES)
class TestCspCompliance:
    def test_no_inline_script(self, name: str, ctx: dict[str, Any]) -> None:
        html = _render(name, ctx)
        m = _INLINE_SCRIPT_RE.search(html)
        assert m is None, (
            f"{name}: inline <script> block at offset {m.start() if m else None}: "
            f"{html[max(0, (m.start() if m else 0) - 30):(m.end() if m else 0) + 30]!r}"
        )

    def test_no_inline_style_block(self, name: str, ctx: dict[str, Any]) -> None:
        html = _render(name, ctx)
        m = _INLINE_STYLE_BLOCK_RE.search(html)
        assert m is None, f"{name}: inline <style> block found"

    def test_no_inline_style_attribute(self, name: str, ctx: dict[str, Any]) -> None:
        html = _render(name, ctx)
        m = _INLINE_STYLE_ATTR_RE.search(html)
        assert (
            m is None
        ), f'{name}: inline style="..." attribute at offset {m.start() if m else None}'

    def test_no_inline_event_handler(self, name: str, ctx: dict[str, Any]) -> None:
        html = _render(name, ctx)
        m = _INLINE_EVENT_RE.search(html)
        assert m is None, f"{name}: inline on*= handler at offset {m.start() if m else None}"


# Special: message_view needs a constructed message object.
def _msg() -> Any:
    return SimpleNamespace(
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


class TestMessageViewCsp:
    def test_no_inline_script_or_style(self) -> None:
        html = _render("message_view.html", _ctx({"message": _msg()}))
        assert _INLINE_SCRIPT_RE.search(html) is None
        assert _INLINE_STYLE_BLOCK_RE.search(html) is None
        assert _INLINE_STYLE_ATTR_RE.search(html) is None
        assert _INLINE_EVENT_RE.search(html) is None
