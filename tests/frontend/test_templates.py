"""Render every Jinja2 template with a typical context. The point is to
catch missing-attr, undefined-name, or syntax issues before they hit the
browser.

Source of truth: ``backend/app/templates/`` directory + ``templates.py``.
"""

from __future__ import annotations

import json
import re
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
    pathlib.Path(__file__).parent.parent.parent / "backend" / "app" / "templates"
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
        out = (
            _env()
            .get_template("set_password.html")
            .render(_ctx({"username": "alice", "flash": None}))
        )
        assert "<form" in out

    def test_inbox(self) -> None:
        out = (
            _env()
            .get_template("inbox.html")
            .render(
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
        )
        assert "<" in out

    def test_compose(self) -> None:
        out = (
            _env()
            .get_template("compose.html")
            .render(
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
        out = _env().get_template("accounts/list.html").render(_ctx({"accounts": []}))
        assert "<" in out

    def test_accounts_form_create(self) -> None:
        out = _env().get_template("accounts/form.html").render(_ctx({"account": None}))
        assert "<form" in out

    def test_admin_users(self) -> None:
        out = (
            _env()
            .get_template("admin/users.html")
            .render(
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
        )
        assert "<" in out

    def test_admin_audit(self) -> None:
        out = (
            _env()
            .get_template("admin/audit.html")
            .render(
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
        )
        assert "<" in out

    @pytest.mark.parametrize("name", ["403.html", "404.html", "4xx.html", "500.html", "5xx.html"])
    def test_error_pages(self, name: str) -> None:
        out = (
            _env()
            .get_template(f"errors/{name}")
            .render(_ctx({"detail": None, "request_id": "rid"}))
        )
        assert "<" in out


# ---------------------------------------------------------------------------
# Inbox account combobox (typeahead) — Sprint: searchable mailbox filter.
#
# The inbox filter renders a searchable combobox (ARIA 1.2 pattern) over the
# *scoped* ``accounts`` list. JS progressively enhances it; without JS a
# <noscript> <select> carries ``account_id``. The combobox ships its options
# as a NON-executable ``<script type="application/json">`` data island so the
# client can filter by email OR display_name (никнейм) substring.
#
# Source of truth: backend/app/templates/inbox.html (combobox block) +
# backend/app/static/js/inbox.js (setupAccountCombobox).
# ---------------------------------------------------------------------------


def _acc(id: int, email: str, display_name: str | None = None) -> SimpleNamespace:
    """A mail-account DTO-ish object as the inbox router passes it.

    Only the attributes the template touches are populated: ``id``,
    ``email``, ``display_name``.
    """
    return SimpleNamespace(id=id, email=email, display_name=display_name)


def _render_inbox(accounts: list[Any], selected_account_id: int | None = None) -> str:
    return (
        _env()
        .get_template("inbox.html")
        .render(
            _ctx(
                {
                    "items": [],
                    "next_cursor": None,
                    "accounts": accounts,
                    "selected_account_id": selected_account_id,
                    "selected_tag_id": None,
                    "selected_group_id": None,
                    "tags": [],
                    "groups": [],
                    "unread_only": False,
                    "unread_count": 0,
                }
            )
        )
    )


def _extract_options_json(html: str) -> Any:
    """Pull the JSON payload out of the data-island <script> block."""
    m = re.search(
        r'<script\s+type="application/json"\s+data-account-options>(.*?)</script>',
        html,
        re.DOTALL,
    )
    assert m is not None, "data-account-options JSON island not found in inbox.html"
    return json.loads(m.group(1))


class TestInboxAccountCombobox:
    def test_combobox_markup_rendered(self) -> None:
        html = _render_inbox([_acc(1, "a@x.com", "Alice")])
        # The progressive-enhancement combobox root + ARIA wiring.
        assert "data-account-combobox" in html
        assert 'role="combobox"' in html
        assert "data-account-input" in html
        assert "data-account-listbox" in html
        # Hidden carrier of account_id, disabled until JS enables it.
        assert "data-account-value" in html
        assert re.search(r'<input[^>]*name="account_id"[^>]*data-account-value', html)

    def test_noscript_select_fallback_present(self) -> None:
        html = _render_inbox([_acc(1, "a@x.com", "Alice"), _acc(2, "b@x.com", None)])
        assert "<noscript>" in html
        # The fallback <select name="account_id"> carries every visible account.
        assert re.search(r'<select[^>]*name="account_id"', html)
        assert 'value="1"' in html
        assert 'value="2"' in html
        assert ">Все почты<" in html  # "all mailboxes" empty option

    def test_json_payload_contains_id_email_name_for_all_accounts(self) -> None:
        accounts = [
            _acc(1, "alice@x.com", "Alice"),
            _acc(2, "bob@x.com", None),  # no display_name -> name = ""
            _acc(3, "carol@x.com", "Carol C"),
        ]
        data = _extract_options_json(_render_inbox(accounts))
        assert isinstance(data, list)
        assert len(data) == 3
        by_id = {row["id"]: row for row in data}
        # Every visible account is present with all three fields.
        assert by_id[1] == {"id": 1, "email": "alice@x.com", "name": "Alice"}
        assert by_id[2] == {"id": 2, "email": "bob@x.com", "name": ""}
        assert by_id[3] == {"id": 3, "email": "carol@x.com", "name": "Carol C"}

    def test_json_payload_empty_when_no_accounts(self) -> None:
        # Empty-state inbox: payload is an empty JSON array, still valid JSON.
        data = _extract_options_json(_render_inbox([]))
        assert data == []

    def test_selected_account_label_shown_in_input(self) -> None:
        accounts = [_acc(1, "alice@x.com", "Alice"), _acc(2, "bob@x.com", None)]
        # With a display_name, effective_account_label uses the nickname.
        html = _render_inbox(accounts, selected_account_id=1)
        assert re.search(r'<input[^>]*data-account-input[^>]*value="Alice"', html) or re.search(
            r'<input[^>]*value="Alice"[^>]*data-account-input', html
        )
        # Hidden carrier reflects the selected id.
        assert re.search(r'<input[^>]*name="account_id"[^>]*value="1"', html)
        # Without a display_name, the label falls back to the email.
        html2 = _render_inbox(accounts, selected_account_id=2)
        assert "bob@x.com" in html2

    def test_clear_button_visible_only_when_account_selected(self) -> None:
        accounts = [_acc(1, "alice@x.com", "Alice")]
        # No selection -> clear (x) button is hidden.
        unsel = _render_inbox(accounts, selected_account_id=None)
        assert re.search(r"<button[^>]*data-account-clear[^>]*\bhidden\b", unsel)
        # Selection -> clear button is shown (no hidden attr).
        sel = _render_inbox(accounts, selected_account_id=1)
        m = re.search(r"<button[^>]*data-account-clear[^>]*>", sel)
        assert m is not None
        assert "hidden" not in m.group(0)

    def test_xss_email_and_display_name_escaped_in_json(self) -> None:
        # Hostile email / display_name must NOT break out of the JSON island
        # nor inject markup. ``tojson`` HTML-escapes </script>, quotes, etc.
        evil = _acc(7, 'a"@x.com</script><script>alert(1)</script>', '<b>"& ')
        html = _render_inbox([evil])
        # No raw closing-script breakout inside the data island.
        island = re.search(
            r'<script\s+type="application/json"\s+data-account-options>(.*?)</script>',
            html,
            re.DOTALL,
        )
        assert island is not None
        body = island.group(1)
        # The literal "</script>" sequence must be escaped (tojson emits <).
        assert "</script>" not in body
        # And the embedded JSON must still parse and round-trip the raw values.
        data = _extract_options_json(html)
        assert data[0]["email"] == 'a"@x.com</script><script>alert(1)</script>'
        assert data[0]["name"] == '<b>"& '
        # No unescaped injected executable <script>alert tag landed in the doc.
        assert "<script>alert(1)</script>" not in html

    def test_xss_display_name_escaped_in_noscript_select(self) -> None:
        # The <noscript> <select> option label goes through effective_account_label
        # which ``| e`` HTML-escapes. A hostile nickname must not inject markup.
        evil = _acc(9, "z@x.com", "<img src=x onerror=alert(1)>")
        html = _render_inbox([evil], selected_account_id=9)
        assert "<img src=x onerror=alert(1)>" not in html
        # Escaped form is present somewhere (input value and/or select option).
        assert "&lt;img src=x onerror=alert(1)&gt;" in html
