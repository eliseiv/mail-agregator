"""Telegram notification + webhook addressing through ``user_groups`` (ADR-0030).

The recipient SQL (``list_recipients_for_message`` / ``list_missing_for_recovery``)
and the webhook tag-predicate (``find_active_for_message`` /
``list_missing_for_recovery``) replaced the single-team predicate
``u.group_id = ma.group_id`` with a membership check over ``user_groups``. A
user in teams [A, B] must therefore be addressed for mail on accounts of BOTH
A and B, and NOT for a third team C.

Verification (plan §Telegram-уведомления + §Webhooks):
- recipients for a message on team A include a member of [A, B];
- the same member is a recipient for a message on team B;
- a member of only [A] / [B] is NOT a recipient for team C mail;
- per-chat recovery (``list_missing_for_recovery``) honours memberships;
- webhook tag-filter (find_active + recovery) honours all memberships.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from backend.app.repositories.telegram_notifications import TelegramNotificationsRepo
from backend.app.repositories.webhooks import WebhookDeliveriesRepo, WebhooksRepo
from tests.multigroup.conftest import MultiGroupSeeder

pytestmark = pytest.mark.integration


def _future() -> datetime:
    # internal_date strictly after the link/webhook created_at so the
    # first-link / history-flood filters pass.
    return datetime.now(UTC) + timedelta(minutes=1)


class TestTelegramRecipients:
    async def test_multi_team_member_is_recipient_for_both_teams(
        self, mseed: MultiGroupSeeder
    ) -> None:
        ga, _la = await mseed.group_with_leader("Team A")
        gb, _lb = await mseed.group_with_leader("Team B")
        m = await mseed.member(ga.id)
        await mseed.membership(user_id=m.id, group_id=gb.id)
        await mseed.telegram_link(user_id=m.id, telegram_user_id=700001)

        acc_a = await mseed.mail_account(user_id=_la.id, group_id=ga.id, email="na@x.com")
        acc_b = await mseed.mail_account(user_id=_lb.id, group_id=gb.id, email="nb@x.com")
        # A tag is required for the recipient SQL to fire (TG_NOTIFY_ALL flag
        # may be off in this env); attach the member's own tag to each message.
        msg_a = await mseed.message(mail_account_id=acc_a.id, internal_date=_future())
        msg_b = await mseed.message(mail_account_id=acc_b.id, internal_date=_future())
        tag = await mseed.tag(user_id=m.id, name="mine", rules=[("subject_contains", "Subject")])
        await mseed.link(message_id=msg_a.id, tag_id=tag.id)
        await mseed.link(message_id=msg_b.id, tag_id=tag.id)

        repo = TelegramNotificationsRepo(mseed.s)
        rec_a = {r.user_id for r in await repo.list_recipients_for_message(message_id=msg_a.id)}
        rec_b = {r.user_id for r in await repo.list_recipients_for_message(message_id=msg_b.id)}
        assert m.id in rec_a, "member of [A,B] must be addressed for team A mail"
        assert m.id in rec_b, "member of [A,B] must be addressed for team B mail"

    async def test_non_member_is_not_recipient_for_third_team(
        self, mseed: MultiGroupSeeder
    ) -> None:
        ga, _la = await mseed.group_with_leader("Team A")
        gb, _lb = await mseed.group_with_leader("Team B")
        gc, _lc = await mseed.group_with_leader("Team C")
        m = await mseed.member(ga.id)
        await mseed.membership(user_id=m.id, group_id=gb.id)
        await mseed.telegram_link(user_id=m.id, telegram_user_id=700002)

        acc_c = await mseed.mail_account(user_id=_lc.id, group_id=gc.id, email="nc@x.com")
        msg_c = await mseed.message(mail_account_id=acc_c.id, internal_date=_future())
        # Give the member a tag, but the message is on team C he does not belong to.
        tag = await mseed.tag(user_id=m.id, name="mine2", rules=[("subject_contains", "Subject")])
        await mseed.link(message_id=msg_c.id, tag_id=tag.id)

        repo = TelegramNotificationsRepo(mseed.s)
        rec_c = {r.user_id for r in await repo.list_recipients_for_message(message_id=msg_c.id)}
        assert m.id not in rec_c, "member must NOT be addressed for a team he is not in"

    async def test_recovery_scan_honours_memberships(self, mseed: MultiGroupSeeder) -> None:
        """``list_missing_for_recovery`` picks up a team-B message for a member
        of [A, B] who has a live chat but no delivery row yet.
        """
        ga, _la = await mseed.group_with_leader("Team A")
        gb, _lb = await mseed.group_with_leader("Team B")
        m = await mseed.member(ga.id)
        await mseed.membership(user_id=m.id, group_id=gb.id)
        await mseed.telegram_link(user_id=m.id, telegram_user_id=700003)

        acc_b = await mseed.mail_account(user_id=_lb.id, group_id=gb.id, email="recb@x.com")
        # fetched_at must be inside the recovery window; internal_date after link.
        msg_b = await mseed.message(mail_account_id=acc_b.id, internal_date=_future())
        tag = await mseed.tag(user_id=m.id, name="rec", rules=[("subject_contains", "Subject")])
        await mseed.link(message_id=msg_b.id, tag_id=tag.id)

        missing = await TelegramNotificationsRepo(mseed.s).list_missing_for_recovery(
            window_hours=24, limit=500
        )
        assert msg_b.id in missing, "team B message recoverable for the [A,B] member"


class TestWebhookAddressing:
    async def test_find_active_uses_membership_predicate(self, mseed: MultiGroupSeeder) -> None:
        """A member of [A, B] owns a tag matching a message on team B's account;
        team B's webhook must fire (tag owner is a member of the mailbox's team
        through ``user_groups``).
        """
        ga, _la = await mseed.group_with_leader("Team A")
        gb, _lb = await mseed.group_with_leader("Team B")
        m = await mseed.member(ga.id)  # home A
        await mseed.membership(user_id=m.id, group_id=gb.id)  # additional B

        acc_b = await mseed.mail_account(user_id=_lb.id, group_id=gb.id, email="whb@x.com")
        await mseed.webhook(group_id=gb.id)
        msg_b = await mseed.message(mail_account_id=acc_b.id, body_text="invoice please")
        # The tag belongs to the multi-team member; auto-tag must attach it
        # because he is a member of team B (the mailbox's team).
        tag = await mseed.tag(user_id=m.id, name="inv", rules=[("body_contains", "invoice")])
        await mseed.apply_tags_to_message(message=msg_b, mail_account_id=acc_b.id)
        assert tag.id in await mseed.tags_on_message(msg_b.id), "tag attaches via membership"

        recipient = await WebhooksRepo(mseed.s).find_active_for_message(
            message_id=msg_b.id, mail_account_id=acc_b.id
        )
        assert recipient is not None, "team B webhook fires (tag owner is a member of B)"
        assert recipient.group_id == gb.id

    async def test_recovery_scan_honours_membership(self, mseed: MultiGroupSeeder) -> None:
        ga, _la = await mseed.group_with_leader("Team A")
        gb, _lb = await mseed.group_with_leader("Team B")
        m = await mseed.member(ga.id)
        await mseed.membership(user_id=m.id, group_id=gb.id)

        acc_b = await mseed.mail_account(user_id=_lb.id, group_id=gb.id, email="whrec@x.com")
        await mseed.webhook(group_id=gb.id)
        msg_b = await mseed.message(mail_account_id=acc_b.id, body_text="contract here")
        tag = await mseed.tag(user_id=m.id, name="con", rules=[("body_contains", "contract")])
        await mseed.apply_tags_to_message(message=msg_b, mail_account_id=acc_b.id)
        assert tag.id in await mseed.tags_on_message(msg_b.id)

        missing = await WebhookDeliveriesRepo(mseed.s).list_missing_for_recovery(
            window_hours=24, limit=500
        )
        assert msg_b.id in missing, "team B tagged message recoverable via membership"
