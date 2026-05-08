"""Make groups.leader_user_id nullable (FE-FIX round-2 #3).

A group can now be created without a leader; the first member added
later (or the first member in the create-group payload) becomes the
leader. The UNIQUE constraint stays so a user can lead at most one
group, but UNIQUE in Postgres treats multiple NULLs as distinct so
this is a no-op for orphan groups.

Revision ID: 20260509_008
Revises: 20260508_007
Create Date: 2026-05-09
"""

from __future__ import annotations

from alembic import op

revision = "20260509_008"
down_revision = "20260508_007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE groups ALTER COLUMN leader_user_id DROP NOT NULL")


def downgrade() -> None:
    # Reverse only works if no orphan rows exist.
    op.execute("ALTER TABLE groups ALTER COLUMN leader_user_id SET NOT NULL")
