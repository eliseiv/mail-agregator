"""Add ``mail_accounts.group_id`` so accounts stay with their original group
when their owner is moved to a different group (FE-FIX round-10).

Until this revision, mail-account visibility was derived from the owning
user's current ``users.group_id``. That made accounts "follow" their
owner — when an admin moved a user from group A to group B, every
account they owned silently became visible to B and invisible to A.

With this revision, ``mail_accounts.group_id`` is the source of truth
for group-scoped visibility, and changing the owner's group no longer
affects which group sees the account. Backfill copies the current
``users.group_id`` of each owner into the new column.

Revision ID: 20260509_009
Revises: 20260509_008
Create Date: 2026-05-09
"""

from __future__ import annotations

from alembic import op

revision = "20260509_009"
down_revision = "20260509_008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1) Add the column nullable so the backfill can happen without a default.
    op.execute(
        "ALTER TABLE mail_accounts "
        "ADD COLUMN group_id BIGINT NULL "
        "REFERENCES groups(id) ON DELETE SET NULL"
    )
    # 2) Backfill: copy each account owner's current group_id into the new column.
    op.execute(
        "UPDATE mail_accounts ma "
        "SET    group_id = u.group_id "
        "FROM   users u "
        "WHERE  ma.user_id = u.id"
    )
    # 3) Index for the visibility filter (`WHERE group_id = ?`).
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_mail_accounts_group_id "
        "ON mail_accounts (group_id)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_mail_accounts_group_id")
    op.execute("ALTER TABLE mail_accounts DROP COLUMN IF EXISTS group_id")
