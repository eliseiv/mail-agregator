"""``messages.pushed_at`` — push-outbox marker for the CRM connector (ADR-0043 §2).

The aggregator becomes a thin mail-connector that **pushes** every newly
synced message to the CRM (`POST {CRM_INGEST_URL}/api/mail/ingest`). The
``messages`` table gains a nullable ``pushed_at TIMESTAMPTZ`` column that acts
as the outbox high-water marker:

- ``pushed_at IS NULL`` — the message has NOT yet been delivered to the CRM
  (fresh from sync, or a delivery that failed / was interrupted).
- ``pushed_at = <ts>`` — the CRM accepted the message (`2xx`); it will not be
  re-pushed.

A **partial index** on ``(id) WHERE pushed_at IS NULL`` keeps the
``crm_push_recovery`` scan (re-enqueue of undelivered messages) cheap: the
scan only ever touches the small, shrinking set of pending rows, never the
full retention window of already-pushed messages.

Additive / forward-only per ``07-deployment.md`` migration policy — no data
backfill (pre-existing rows stay ``NULL`` and are picked up by the recovery
scan once the CRM push is configured). Nothing is dropped in this sprint
(strip/decommission is a later sprint, ADR-0043 §5).

Revision ID: 20260710_024
Revises: 20260709_023
Create Date: 2026-07-10
"""

from __future__ import annotations

from alembic import op

revision = "20260710_024"
down_revision = "20260709_023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS pushed_at TIMESTAMPTZ NULL")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_messages_pushed_at_pending "
        "ON messages (id) WHERE pushed_at IS NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_messages_pushed_at_pending")
    op.execute("ALTER TABLE messages DROP COLUMN IF EXISTS pushed_at")
