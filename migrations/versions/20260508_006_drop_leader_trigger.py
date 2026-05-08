"""Drop users_group_leader_consistency_check trigger.

Same reason as 20260508_005: the trigger fires immediately on INSERT and
makes the auto-create-leader flow impossible (user is created first with
group_id=NULL, then group is created and user.group_id is filled in the
same transaction). Constraint triggers CAN be DEFERRABLE but switching
the trigger to DEFERRABLE is non-trivial across alembic; the service
layer already enforces the same invariant before the SQL hits.

Revision ID: 20260508_006
Revises: 20260508_005
Create Date: 2026-05-08
"""

from __future__ import annotations

from alembic import op

revision = "20260508_006"
down_revision = "20260508_005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS users_group_leader_consistency_check ON users")
    op.execute("DROP FUNCTION IF EXISTS users_group_leader_consistency_check_fn() CASCADE")


def downgrade() -> None:
    # No-op restoration; reinstating the trigger requires ADR-0019 §6 wording.
    pass
