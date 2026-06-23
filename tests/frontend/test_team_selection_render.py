"""Frontend render tests for mailbox team selection / transfer (ADR-0031).

Renders the production Jinja2 templates ``accounts/form.html`` and
``accounts/list.html`` with the exact context shapes the router passes
(``_team_selector_context``) and asserts the visibility rules from ADR-0031
§2/§4.6/§4.7:

- single-team non-admin: NO team selector on the add form, NO transfer action;
- multi-team: selector rendered, options = teams, default = home_group_id;
- group_member: transfer action never rendered;
- super_admin: "Без команды" (no-team) option present.

Source of truth: backend/app/templates/accounts/form.html + list.html,
backend/app/accounts/router.py ``_team_selector_context``.
"""

from __future__ import annotations

import re
from types import SimpleNamespace
from typing import Any

import pytest

# Reuse the production-identical Jinja env + base context builder.
from tests.frontend.test_templates import _ctx, _env

pytestmark = pytest.mark.frontend


def _acc(
    id: int,
    email: str,
    *,
    display_name: str | None = None,
    is_active: bool = True,
    auth_type: str = "password",
) -> SimpleNamespace:
    return SimpleNamespace(
        id=id,
        email=email,
        display_name=display_name,
        is_active=is_active,
        auth_type=auth_type,
        oauth_needs_consent=False,
        provider_label=None,
        last_synced_at=None,
        last_sync_error=None,
    )


def _render_form(extra: dict[str, Any]) -> str:
    base = {"account": None}
    base.update(extra)
    return _env().get_template("accounts/form.html").render(_ctx(base))


def _render_list(extra: dict[str, Any]) -> str:
    base: dict[str, Any] = {"accounts": []}
    base.update(extra)
    return _env().get_template("accounts/list.html").render(_ctx(base))


# ===========================================================================
# Add form — team selector (ADR-0031 §2)
# ===========================================================================


class TestAddFormSelector:
    def test_single_team_member_no_selector(self) -> None:
        """One team, not super_admin → no team <select> on the add form."""
        html = _render_form(
            {
                "teams": [{"id": 1, "name": "Alpha"}],
                "home_group_id": 1,
                "is_super_admin": False,
                "is_group_member": True,
            }
        )
        assert "data-account-group" not in html
        assert 'name="group_id"' not in html

    def test_multi_team_selector_rendered_with_options(self) -> None:
        """≥2 teams → selector present; one <option> per team."""
        html = _render_form(
            {
                "teams": [{"id": 1, "name": "Alpha"}, {"id": 2, "name": "Beta"}],
                "home_group_id": 2,
                "is_super_admin": False,
                "is_group_member": True,
            }
        )
        assert "data-account-group" in html
        assert re.search(r'<select[^>]*name="group_id"', html)
        assert re.search(r'<option value="1"[^>]*>\s*Alpha', html)
        assert re.search(r'<option value="2"[^>]*>\s*Beta', html)
        # No "Без команды" option for a non-super_admin.
        assert "Без команды" not in html

    def test_multi_team_default_is_home_group(self) -> None:
        """The home team option carries the ``selected`` attribute."""
        html = _render_form(
            {
                "teams": [{"id": 1, "name": "Alpha"}, {"id": 2, "name": "Beta"}],
                "home_group_id": 2,
                "is_super_admin": False,
                "is_group_member": True,
            }
        )
        # Option for id=2 (home) is selected; id=1 is not.
        m2 = re.search(r'<option value="2"[^>]*>', html)
        m1 = re.search(r'<option value="1"[^>]*>', html)
        assert m2 is not None and "selected" in m2.group(0)
        assert m1 is not None and "selected" not in m1.group(0)

    def test_super_admin_has_no_team_option(self) -> None:
        """super_admin: "Без команды" (group_id=NULL) option present + selected
        when home is None; selector visible even with a single team."""
        html = _render_form(
            {
                "teams": [{"id": 5, "name": "Solo"}],
                "home_group_id": None,
                "is_super_admin": True,
                "is_group_member": False,
            }
        )
        assert "data-account-group" in html
        assert re.search(r'<option value=""[^>]*>\s*Без команды', html)
        # With home_group_id None, the empty option is the selected default.
        empty_opt = re.search(r'<option value=""[^>]*>', html)
        assert empty_opt is not None and "selected" in empty_opt.group(0)

    def test_selector_absent_on_edit_form(self) -> None:
        """ADR-0031 §4.7: edit form never renders the team selector."""
        acc = _acc(1, "a@x.com")
        html = (
            _env()
            .get_template("accounts/form.html")
            .render(
                _ctx(
                    {
                        "account": acc,
                        "teams": [{"id": 1, "name": "Alpha"}, {"id": 2, "name": "Beta"}],
                        "home_group_id": 1,
                        "is_super_admin": True,
                        "is_group_member": False,
                    }
                )
            )
        )
        assert "data-account-group" not in html


# ===========================================================================
# List page — transfer action (ADR-0031 §3/§4.6)
# ===========================================================================


class TestListTransferAction:
    def _ctx_extra(
        self,
        *,
        teams: list[dict[str, Any]],
        is_member: bool,
        is_super: bool,
        current_group: int | None = 1,
    ) -> dict[str, Any]:
        acc = _acc(7, "box@x.com")
        return {
            "accounts": [acc],
            "account_group": {7: current_group},
            "teams": teams,
            "is_group_member": is_member,
            "is_super_admin": is_super,
            "scope": SimpleNamespace(is_super_admin=is_super),
        }

    def test_group_member_no_transfer_action(self) -> None:
        """group_member: transfer action never rendered, even multi-team."""
        html = _render_list(
            self._ctx_extra(
                teams=[{"id": 1, "name": "Alpha"}, {"id": 2, "name": "Beta"}],
                is_member=True,
                is_super=False,
            )
        )
        assert "Перенести в другую команду" not in html
        assert "data-account-transfer-form" not in html

    def test_single_team_non_member_no_transfer_action(self) -> None:
        """Leader with a single team → no transfer (no second target)."""
        html = _render_list(
            self._ctx_extra(
                teams=[{"id": 1, "name": "Alpha"}],
                is_member=False,
                is_super=False,
            )
        )
        assert "Перенести в другую команду" not in html
        assert "data-account-transfer-form" not in html

    def test_multi_team_leader_transfer_rendered(self) -> None:
        """Leader with ≥2 teams → transfer form present with team options."""
        html = _render_list(
            self._ctx_extra(
                teams=[{"id": 1, "name": "Alpha"}, {"id": 2, "name": "Beta"}],
                is_member=False,
                is_super=False,
            )
        )
        assert "Перенести в другую команду" in html
        assert "data-account-transfer-form" in html
        assert re.search(r'<select[^>]*name="group_id"', html)
        # PATCH via form-fallback (method override), no new route.
        assert re.search(r'<input[^>]*name="_method"[^>]*value="PATCH"', html)
        assert 'action="/api/mail-accounts/7"' in html

    def test_super_admin_transfer_has_no_team_option(self) -> None:
        """super_admin with ≥1 team → transfer rendered + "Без команды" option."""
        html = _render_list(
            self._ctx_extra(
                teams=[{"id": 1, "name": "Alpha"}],
                is_member=False,
                is_super=True,
                current_group=1,
            )
        )
        assert "data-account-transfer-form" in html
        assert re.search(r'<option value=""[^>]*>\s*Без команды', html)

    def test_transfer_marks_current_team_selected(self) -> None:
        """The account's current team is pre-selected and labelled (текущая)."""
        html = _render_list(
            self._ctx_extra(
                teams=[{"id": 1, "name": "Alpha"}, {"id": 2, "name": "Beta"}],
                is_member=False,
                is_super=False,
                current_group=2,
            )
        )
        m2 = re.search(r'<option value="2"[^>]*>(.*?)</option>', html, re.DOTALL)
        assert m2 is not None
        assert "selected" in m2.group(0)
        assert "текущая" in m2.group(1)
