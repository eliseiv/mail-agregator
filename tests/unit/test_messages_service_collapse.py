"""Unit test: :meth:`MessageService.get` normalises the displayed body
(ADR-0022 §2.10, round-37) while leaving the STORED body untouched.

This is a pure-logic test of the service's assembly step: the repository /
accounts / users / tags collaborators are mocked (they are the I/O boundary,
not the unit under test). We feed a "dirty" stored message — a tall column of
blank lines in both ``body_text`` and ``body_html`` — and assert:

1. The returned ``MessageDetail.body_text`` / ``.body_html`` are collapsed.
2. The stored ORM object's ``body_text`` / ``body_html`` attributes are NOT
   mutated (the collapse is render-time only — matching reads the raw value).

Regression guard for the "matching reads raw body" invariant lives in the SQL
tag-matching suite (``tests/tags/test_body_html_matching_sql.py`` and
``test_tag_matching_sql.py``) which exercises the worker auto-tag + apply paths
directly against the raw stored bodies, never via ``MessageService.get``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.app.messages.service import MessageService

pytestmark = pytest.mark.unit


# A stored body with the Apple-style tall column of blank lines.
DIRTY_BODY_TEXT = "Hello there." + "\n" * 12 + "Best regards."
DIRTY_BODY_HTML = "<p>Hello there.</p>" + "<p>&nbsp;</p>" * 6 + "<p>Best regards.</p>"


def _build_service_with_dirty_message() -> tuple[MessageService, SimpleNamespace]:
    """Construct a MessageService whose collaborators are mocked to return a
    single dirty stored message. Returns (service, stored_msg) so the caller can
    assert the stored object was not mutated.
    """
    now = datetime(2026, 6, 1, tzinfo=UTC)
    stored_msg = SimpleNamespace(
        id=42,
        mail_account_id=7,
        from_addr="boss@corp.com",
        from_name="Boss",
        to_addrs="me@example.com",
        cc_addrs=None,
        subject="Quarterly report",
        internal_date=now,
        body_text=DIRTY_BODY_TEXT,
        body_html=DIRTY_BODY_HTML,
        body_truncated=False,
        body_present=True,
        in_reply_to=None,
        is_read=False,
    )
    account = SimpleNamespace(id=7, email="inbox@example.com", display_name="Inbox", user_id=3)
    owner = SimpleNamespace(id=3, username="owner", display_name="Owner")

    # MessageService.__init__ builds its own repos + storage from the session;
    # we bypass __init__ and inject mocked collaborators directly.
    svc = MessageService.__new__(MessageService)
    svc._db = MagicMock()

    repo = MagicMock()
    repo.get_for_user_ids = AsyncMock(return_value=stored_msg)
    repo.list_attachments_bulk = AsyncMock(return_value={42: []})
    svc._repo = repo

    accounts = MagicMock()
    accounts.get_by_id = AsyncMock(return_value=account)
    # visible_user_ids() (called inside get) hits this; return None = no filter.
    accounts.list_canonical_account_ids = AsyncMock(return_value=None)
    svc._accounts = accounts

    users = MagicMock()
    users.get_by_id = AsyncMock(return_value=owner)
    svc._users = users

    tags = MagicMock()
    tags.list_for_message = AsyncMock(return_value=[])
    svc._tags = tags

    svc._storage = MagicMock()
    return svc, stored_msg


def _super_admin_scope() -> Any:
    # visible_user_ids takes the super-admin (no group) path → list_canonical…
    return SimpleNamespace(is_super_admin=True, group_id=None, user_id=3)


class TestMessageDetailBodyNormalised:
    async def test_get_collapses_body_text_and_html_in_dto(self) -> None:
        svc, _ = _build_service_with_dirty_message()
        detail = await svc.get(scope=_super_admin_scope(), message_id=42)

        # body_text: tall column collapsed to a single paragraph separator.
        assert detail.body_text == "Hello there.\n\nBest regards."
        assert "\n\n\n" not in detail.body_text

        # body_html: empty <p>&nbsp;</p> separators removed, content preserved.
        assert "<p>&nbsp;</p>" not in (detail.body_html or "")
        assert "<p>Hello there.</p>" in (detail.body_html or "")
        assert "<p>Best regards.</p>" in (detail.body_html or "")

    async def test_get_does_not_mutate_stored_body(self) -> None:
        """The collapse is render-time only: the ORM object handed back by the
        repository must keep its raw body (tag-matching / push-preview read it).
        """
        svc, stored_msg = _build_service_with_dirty_message()
        await svc.get(scope=_super_admin_scope(), message_id=42)

        assert stored_msg.body_text == DIRTY_BODY_TEXT
        assert stored_msg.body_html == DIRTY_BODY_HTML
