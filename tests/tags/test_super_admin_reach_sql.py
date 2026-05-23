"""super_admin reach + webhook channel isolation — round-28.

Two coupled changes are under test:

1. ``APPLY_TAGS_TO_MESSAGE`` gained ``OR u.role = 'super_admin'`` in its
   visibility join (ADR-0017 §5.1): a super-admin's *personal* tag attaches
   to EVERY incoming message, including mail on another team's account, so
   the super-admin gets a Telegram notification (the recipient SQL already
   has a super_admin branch).

2. The webhook repo SELECTs (``find_active_for_message``,
   ``list_tags_for_team``, ``list_missing_for_recovery``) had super_admin
   REMOVED from their tag predicate (ADR-0023 §3.2): a message tagged ONLY
   by a super_admin personal tag must NOT trigger another team's webhook,
   nor leak the super_admin tag's name/colour into that team's payload.

Tests run the production SQL directly against Postgres via the rolled-back
``db_session``. The recipient SQL is exercised through the real repo method
:meth:`TelegramNotificationsRepo.list_recipients_for_message`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from backend.app.repositories.telegram_notifications import (
    TelegramNotificationsRepo,
)
from backend.app.repositories.webhooks import WebhookDeliveriesRepo, WebhooksRepo
from shared.models import TelegramLink
from tests.tags.conftest import Seeder

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# B1. super_admin auto-tag reaches another team's message
# ---------------------------------------------------------------------------


class TestSuperAdminAutoTagReach:
    async def test_super_admin_personal_tag_attaches_to_foreign_team_message(
        self, seed: Seeder
    ) -> None:
        """Mail arrives on an account owned by a member of TEAM A. A
        super-admin (no group) has a personal tag whose rule matches. After
        APPLY_TAGS_TO_MESSAGE the super-admin's tag is on the message.
        """
        g, leader = await seed.group_with_leader("Team A")
        member = await seed.member(g.id)
        acc = await seed.mail_account(user_id=member.id, group_id=g.id, email="teama@x.com")
        msg = await seed.message(
            mail_account_id=acc.id, body_text="please review the invoice attached"
        )

        sa = await seed.super_admin()
        sa_tag = await seed.tag(
            user_id=sa.id, name="sa-invoice", rules=[("body_contains", "invoice")]
        )

        await seed.apply_tags_to_message(message=msg, mail_account_id=acc.id)

        assert sa_tag.id in await seed.tags_on_message(msg.id)

    async def test_super_admin_tag_drives_tg_recipient(self, seed: Seeder) -> None:
        """After auto-tag, the recipient SQL (list_recipients_for_message)
        includes the super-admin so a TG-notification will fire — honouring
        the ``m.internal_date >= tl.created_at`` first-link filter.
        """
        g, leader = await seed.group_with_leader("Team A")
        member = await seed.member(g.id)
        acc = await seed.mail_account(user_id=member.id, group_id=g.id, email="teama2@x.com")
        # internal_date strictly after the link's created_at so the filter passes.
        msg = await seed.message(
            mail_account_id=acc.id,
            body_text="urgent invoice inside",
            internal_date=datetime.now(UTC) + timedelta(minutes=1),
        )

        sa = await seed.super_admin()
        sa_tag = await seed.tag(
            user_id=sa.id, name="sa-urgent", rules=[("body_contains", "invoice")]
        )
        # super-admin has an active telegram link created in the past.
        seed.s.add(
            TelegramLink(
                telegram_user_id=999001,
                user_id=sa.id,
                created_at=datetime.now(UTC) - timedelta(hours=1),
            )
        )
        await seed.s.flush()

        await seed.apply_tags_to_message(message=msg, mail_account_id=acc.id)
        assert sa_tag.id in await seed.tags_on_message(msg.id)

        recipients = await TelegramNotificationsRepo(seed.s).list_recipients_for_message(
            message_id=msg.id
        )
        recipient_ids = {r.user_id for r in recipients}
        assert sa.id in recipient_ids, "super-admin should receive a TG notification"

    async def test_internal_date_before_link_excludes_recipient(self, seed: Seeder) -> None:
        """Defensive: a message older than the super-admin's TG link is not a
        recipient (first-link backfill filter), even though the tag attaches.
        """
        g, leader = await seed.group_with_leader("Team A")
        member = await seed.member(g.id)
        acc = await seed.mail_account(user_id=member.id, group_id=g.id, email="teama3@x.com")
        msg = await seed.message(
            mail_account_id=acc.id,
            body_text="invoice from last year",
            internal_date=datetime.now(UTC) - timedelta(days=2),
        )
        sa = await seed.super_admin()
        await seed.tag(user_id=sa.id, name="sa-old", rules=[("body_contains", "invoice")])
        seed.s.add(
            TelegramLink(
                telegram_user_id=999002,
                user_id=sa.id,
                created_at=datetime.now(UTC) - timedelta(hours=1),
            )
        )
        await seed.s.flush()

        await seed.apply_tags_to_message(message=msg, mail_account_id=acc.id)
        recipients = await TelegramNotificationsRepo(seed.s).list_recipients_for_message(
            message_id=msg.id
        )
        assert sa.id not in {r.user_id for r in recipients}


# ---------------------------------------------------------------------------
# B2. Webhook isolation — super_admin tag must NOT leak into a team webhook
# ---------------------------------------------------------------------------


class TestWebhookIsolationFromSuperAdmin:
    async def test_find_active_returns_none_when_only_super_admin_tag(self, seed: Seeder) -> None:
        """A message on TEAM A tagged ONLY by a super_admin personal tag must
        NOT match find_active_for_message — the team webhook stays silent.
        """
        g, leader = await seed.group_with_leader("Team A")
        member = await seed.member(g.id)
        acc = await seed.mail_account(user_id=member.id, group_id=g.id, email="iso1@x.com")
        msg = await seed.message(mail_account_id=acc.id, body_text="invoice xyz")
        wh = await seed.webhook(group_id=g.id)

        sa = await seed.super_admin()
        sa_tag = await seed.tag(user_id=sa.id, name="sa-iso", rules=[("body_contains", "invoice")])
        # auto-tag: only the super-admin's tag attaches (no team tag matches).
        await seed.apply_tags_to_message(message=msg, mail_account_id=acc.id)
        assert sa_tag.id in await seed.tags_on_message(msg.id)

        recipient = await WebhooksRepo(seed.s).find_active_for_message(
            message_id=msg.id, mail_account_id=acc.id
        )
        assert recipient is None, "super_admin tag must not trigger the team webhook"
        assert wh.id  # webhook exists but is not selected

    async def test_list_tags_for_team_excludes_super_admin_tag(self, seed: Seeder) -> None:
        """Even when a team tag DOES fire (so the webhook is selected), the
        super_admin personal tag must not appear in the outgoing payload.
        """
        g, leader = await seed.group_with_leader("Team A")
        member = await seed.member(g.id)
        acc = await seed.mail_account(user_id=member.id, group_id=g.id, email="iso2@x.com")
        msg = await seed.message(mail_account_id=acc.id, body_text="invoice and contract")
        await seed.webhook(group_id=g.id)

        # member's team tag matches 'contract'
        team_tag = await seed.tag(
            user_id=member.id, name="contract", rules=[("body_contains", "contract")]
        )
        # super_admin tag matches 'invoice'
        sa = await seed.super_admin()
        sa_tag = await seed.tag(
            user_id=sa.id, name="sa-secret", rules=[("body_contains", "invoice")]
        )
        await seed.apply_tags_to_message(message=msg, mail_account_id=acc.id)

        on_msg = await seed.tags_on_message(msg.id)
        assert team_tag.id in on_msg and sa_tag.id in on_msg, "both tags attached"

        payload_tags = await WebhookDeliveriesRepo(seed.s).list_tags_for_team(
            message_id=msg.id, group_id=g.id
        )
        ids = {t.id for t in payload_tags}
        assert team_tag.id in ids, "team tag belongs in the payload"
        assert sa_tag.id not in ids, "super_admin personal tag must NOT leak into payload"

    async def test_recovery_scan_skips_super_admin_only_tag(self, seed: Seeder) -> None:
        """list_missing_for_recovery must not re-enqueue a message that has
        ONLY a super_admin personal tag (would churn forever, then drop).
        """
        g, leader = await seed.group_with_leader("Team A")
        member = await seed.member(g.id)
        acc = await seed.mail_account(user_id=member.id, group_id=g.id, email="iso3@x.com")
        msg = await seed.message(mail_account_id=acc.id, body_text="invoice only")
        await seed.webhook(group_id=g.id)

        sa = await seed.super_admin()
        await seed.tag(user_id=sa.id, name="sa-rec", rules=[("body_contains", "invoice")])
        await seed.apply_tags_to_message(message=msg, mail_account_id=acc.id)

        missing = await WebhookDeliveriesRepo(seed.s).list_missing_for_recovery(
            window_hours=24, limit=100
        )
        assert msg.id not in missing


# ---------------------------------------------------------------------------
# B3. Legitimate team flow still works (no regression)
# ---------------------------------------------------------------------------


class TestLegitimateTeamFlow:
    async def test_team_member_tag_triggers_webhook_and_payload(self, seed: Seeder) -> None:
        g, leader = await seed.group_with_leader("Team A")
        member = await seed.member(g.id)
        acc = await seed.mail_account(user_id=member.id, group_id=g.id, email="legit1@x.com")
        msg = await seed.message(mail_account_id=acc.id, body_text="please sign the contract")
        await seed.webhook(group_id=g.id)

        team_tag = await seed.tag(
            user_id=member.id, name="contract", rules=[("body_contains", "contract")]
        )
        await seed.apply_tags_to_message(message=msg, mail_account_id=acc.id)
        assert team_tag.id in await seed.tags_on_message(msg.id)

        recipient = await WebhooksRepo(seed.s).find_active_for_message(
            message_id=msg.id, mail_account_id=acc.id
        )
        assert recipient is not None, "team tag must trigger the team webhook"
        assert recipient.group_id == g.id

        payload_tags = await WebhookDeliveriesRepo(seed.s).list_tags_for_team(
            message_id=msg.id, group_id=g.id
        )
        assert team_tag.id in {t.id for t in payload_tags}

    async def test_team_message_appears_in_recovery_scan(self, seed: Seeder) -> None:
        g, leader = await seed.group_with_leader("Team A")
        member = await seed.member(g.id)
        acc = await seed.mail_account(user_id=member.id, group_id=g.id, email="legit2@x.com")
        msg = await seed.message(mail_account_id=acc.id, body_text="contract attached")
        await seed.webhook(group_id=g.id)

        team_tag = await seed.tag(
            user_id=member.id, name="contract2", rules=[("body_contains", "contract")]
        )
        await seed.apply_tags_to_message(message=msg, mail_account_id=acc.id)
        assert team_tag.id in await seed.tags_on_message(msg.id)

        missing = await WebhookDeliveriesRepo(seed.s).list_missing_for_recovery(
            window_hours=24, limit=100
        )
        assert msg.id in missing, "team-tagged message with no delivery row must be recoverable"

    async def test_history_flood_filter_excludes_old_message(self, seed: Seeder) -> None:
        """find_active_for_message honours ``m.internal_date >= w.created_at``:
        a message older than the webhook does not fire (round-13 symmetry).
        """
        g, leader = await seed.group_with_leader("Team A")
        member = await seed.member(g.id)
        acc = await seed.mail_account(user_id=member.id, group_id=g.id, email="legit3@x.com")
        # message dated before the webhook is created.
        msg = await seed.message(
            mail_account_id=acc.id,
            body_text="old contract",
            internal_date=datetime.now(UTC) - timedelta(days=10),
        )
        wh = await seed.webhook(group_id=g.id)
        # force webhook created_at to "now" (after the message internal_date).
        await seed.s.execute(
            text("UPDATE webhooks SET created_at = now() WHERE id = :id"), {"id": wh.id}
        )
        team_tag = await seed.tag(
            user_id=member.id, name="contract3", rules=[("body_contains", "contract")]
        )
        await seed.apply_tags_to_message(message=msg, mail_account_id=acc.id)
        assert team_tag.id in await seed.tags_on_message(msg.id)

        recipient = await WebhooksRepo(seed.s).find_active_for_message(
            message_id=msg.id, mail_account_id=acc.id
        )
        assert recipient is None, "message older than webhook must not fire (history flood)"
