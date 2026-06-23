"""Web visibility for a multi-team member (ADR-0030 §2).

A user who is a member of teams [A, B] must see the mailboxes AND messages of
BOTH teams in the web UI, but NOT a third team C. The owner still sees their
personal (orphan) account; super_admin sees everything (deduped by email).

These run the real service layer (``MailAccountService.list_for_scope`` /
``MessageService.list_for_scope``) over a scope built by ``build_scope`` —
i.e. exactly what the request path produces, just without the HTTP shell.

Verification (plan §Visibility веб):
- member of 2 teams sees mailboxes + messages of both;
- does NOT see the third team's messages;
- owner sees their personal mailbox;
- super_admin sees all;
- dedup of mailboxes by email is preserved.
"""

from __future__ import annotations

import pytest

from backend.app.accounts.service import MailAccountService
from backend.app.deps import build_scope
from backend.app.messages.service import MessageService
from tests.multigroup.conftest import MultiGroupSeeder

pytestmark = pytest.mark.integration


async def _scope_for(seed: MultiGroupSeeder, user):  # type: ignore[no-untyped-def]
    return await build_scope(user, seed.s)


class TestMultiTeamMailboxVisibility:
    async def test_member_of_two_teams_sees_both_mailboxes_not_third(
        self, mseed: MultiGroupSeeder
    ) -> None:
        ga, _la = await mseed.group_with_leader("Team A")
        gb, _lb = await mseed.group_with_leader("Team B")
        gc, _lc = await mseed.group_with_leader("Team C")

        # The multi-team member: home in A, additional in B.
        m = await mseed.member(ga.id)
        await mseed.membership(user_id=m.id, group_id=gb.id)

        acc_a = await mseed.mail_account(user_id=_la.id, group_id=ga.id, email="a@x.com")
        acc_b = await mseed.mail_account(user_id=_lb.id, group_id=gb.id, email="b@x.com")
        acc_c = await mseed.mail_account(user_id=_lc.id, group_id=gc.id, email="c@x.com")

        scope = await _scope_for(mseed, m)
        assert scope.group_ids == frozenset({ga.id, gb.id})

        accounts = await MailAccountService(mseed.s).list_for_scope(scope)
        visible_ids = {a.id for a in accounts}
        assert acc_a.id in visible_ids, "team A mailbox visible"
        assert acc_b.id in visible_ids, "team B mailbox visible (additional membership)"
        assert acc_c.id not in visible_ids, "team C mailbox must NOT be visible"

    async def test_member_of_two_teams_sees_messages_of_both_not_third(
        self, mseed: MultiGroupSeeder
    ) -> None:
        ga, _la = await mseed.group_with_leader("Team A")
        gb, _lb = await mseed.group_with_leader("Team B")
        gc, _lc = await mseed.group_with_leader("Team C")

        m = await mseed.member(ga.id)
        await mseed.membership(user_id=m.id, group_id=gb.id)

        acc_a = await mseed.mail_account(user_id=_la.id, group_id=ga.id, email="ma@x.com")
        acc_b = await mseed.mail_account(user_id=_lb.id, group_id=gb.id, email="mb@x.com")
        acc_c = await mseed.mail_account(user_id=_lc.id, group_id=gc.id, email="mc@x.com")
        msg_a = await mseed.message(mail_account_id=acc_a.id, subject="A subj")
        msg_b = await mseed.message(mail_account_id=acc_b.id, subject="B subj")
        msg_c = await mseed.message(mail_account_id=acc_c.id, subject="C subj")

        scope = await _scope_for(mseed, m)
        resp = await MessageService(mseed.s).list_for_scope(
            scope, account_id=None, unread=None, cursor=None, limit=50
        )
        seen = {item.id for item in resp.items}
        assert msg_a.id in seen
        assert msg_b.id in seen
        assert msg_c.id not in seen, "third team's message must not leak"

    async def test_owner_sees_personal_orphan_mailbox(self, mseed: MultiGroupSeeder) -> None:
        ga, _la = await mseed.group_with_leader("Team A")
        m = await mseed.member(ga.id)
        # Personal account created while orphaned -> group_id is NULL.
        personal = await mseed.mail_account(user_id=m.id, group_id=None, email="personal@x.com")

        scope = await _scope_for(mseed, m)
        accounts = await MailAccountService(mseed.s).list_for_scope(scope)
        assert personal.id in {a.id for a in accounts}, "owner always sees their personal mailbox"

    async def test_super_admin_sees_all_mailboxes(self, mseed: MultiGroupSeeder) -> None:
        ga, _la = await mseed.group_with_leader("Team A")
        gb, _lb = await mseed.group_with_leader("Team B")
        acc_a = await mseed.mail_account(user_id=_la.id, group_id=ga.id, email="sa-a@x.com")
        acc_b = await mseed.mail_account(user_id=_lb.id, group_id=gb.id, email="sa-b@x.com")

        sa = await mseed.super_admin()
        scope = await _scope_for(mseed, sa)
        assert scope.is_super_admin
        assert scope.group_ids == frozenset()

        accounts = await MailAccountService(mseed.s).list_for_scope(scope)
        visible = {a.id for a in accounts}
        assert acc_a.id in visible and acc_b.id in visible

    async def test_super_admin_mailbox_dedup_by_email_preserved(
        self, mseed: MultiGroupSeeder
    ) -> None:
        """Two teams added the same mailbox; super_admin sees ONE row (round-18).

        ADR-0030 must not regress the email-dedup for the unscoped super_admin
        view.
        """
        ga, _la = await mseed.group_with_leader("Team A")
        gb, _lb = await mseed.group_with_leader("Team B")
        dup1 = await mseed.mail_account(user_id=_la.id, group_id=ga.id, email="dup@x.com")
        dup2 = await mseed.mail_account(user_id=_lb.id, group_id=gb.id, email="DUP@x.com")

        sa = await mseed.super_admin()
        scope = await _scope_for(mseed, sa)
        accounts = await MailAccountService(mseed.s).list_for_scope(scope)
        dup_rows = [a for a in accounts if a.email.lower() == "dup@x.com"]
        assert len(dup_rows) == 1, "super_admin must see exactly one row per email"
        # Canonical = lowest id.
        assert dup_rows[0].id == min(dup1.id, dup2.id)
