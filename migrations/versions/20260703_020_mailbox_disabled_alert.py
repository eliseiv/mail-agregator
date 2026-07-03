"""Add ``mail_accounts.disabled_alert_sent_at`` (ADR-0033 mailbox-down alert).

Introduces the idempotency stamp for the "mailbox auto-disabled" Telegram
alert. ``NULL`` = no outstanding alert (normal state of an active mailbox);
``!= NULL`` = an alert for the current disabled state was already enqueued
(the moment of enqueue). The worker sets it ``= now()`` guarded (``WHERE
disabled_alert_sent_at IS NULL``) in the same transaction as ``is_active=false``
so exactly one alert is enqueued per Active→Disabled transition (ADR-0033 §2).

No data migration: every existing row starts ``NULL`` = "no outstanding
alert". Already-disabled mailboxes (before this feature) stay ``NULL`` and do
NOT generate a retroactive alert — the feature is proactive from rollout; a
repeat is only possible via re-enable → new disable. Forward-only per
``07-deployment.md`` migration policy.

Revision ID: 20260703_020
Revises: 20260623_019
Create Date: 2026-07-03
"""

from __future__ import annotations

from alembic import op

revision = "20260703_020"
down_revision = "20260623_019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE mail_accounts ADD COLUMN disabled_alert_sent_at TIMESTAMPTZ NULL")


def downgrade() -> None:
    op.execute("ALTER TABLE mail_accounts DROP COLUMN disabled_alert_sent_at")
