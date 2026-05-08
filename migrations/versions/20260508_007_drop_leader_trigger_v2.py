"""Drop trg_users_group_leader_consistency (DEFERRED trigger still fires).

Despite being marked DEFERRABLE INITIALLY DEFERRED, the trigger still
breaks the auto-create-leader flow in some transactional patterns. The
service layer (admin + groups) enforces the invariant before the SQL
hits, so we drop the DB-level enforcement entirely.

Revision ID: 20260508_007
Revises: 20260508_006
Create Date: 2026-05-08
"""

from __future__ import annotations

from alembic import op

revision = "20260508_007"
down_revision = "20260508_006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_users_group_leader_consistency ON users")
    op.execute(
        "DROP FUNCTION IF EXISTS trg_users_group_leader_consistency_fn() CASCADE"
    )
    # The function may also be named without the trg_ prefix in some envs.
    op.execute("DROP FUNCTION IF EXISTS users_group_leader_consistency_fn() CASCADE")


def downgrade() -> None:
    pass
