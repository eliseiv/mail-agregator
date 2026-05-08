"""Drop users_role_group_invariant CHECK (auto-create-leader flow needs it deferrable).

The constraint added in 20260508_004 fires on INSERT of a group_leader because
the user is created first (with group_id=NULL) and the group's group_id is
filled by the immediately-following UPDATE in the same transaction. Postgres
CHECK constraints are not DEFERRABLE — only constraint triggers are. We rely
on backend service-layer validation (admin/groups services enforce the same
invariant before the SQL hits) and remove the database-level CHECK to unblock
the leader/group creation flow.

Revision ID: 20260508_005
Revises: 20260508_004
Create Date: 2026-05-08
"""

from __future__ import annotations

from alembic import op

revision = "20260508_005"
down_revision = "20260508_004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE users DROP CONSTRAINT IF EXISTS users_role_group_invariant")


def downgrade() -> None:
    # Re-add as NOT VALID so historical rows don't fail validation.
    op.execute(
        "ALTER TABLE users ADD CONSTRAINT users_role_group_invariant CHECK ("
        "(role = 'super_admin' AND group_id IS NULL) OR "
        "(role IN ('group_leader', 'group_member') AND group_id IS NOT NULL)"
        ") NOT VALID"
    )
