"""ADR-0044 Phase F — reduce ``users`` to the single ``crm-service`` row.

Runbook: ``docs/adr/ADR-0044-decommission-runbook.md`` §4 (Phase F).

``DELETE FROM users WHERE username <> 'crm-service'``. Safe now because:

- every mailbox was repointed onto ``crm-service`` in Phase C, so the
  ``mail_accounts.user_id NOT NULL CASCADE`` FK deletes no mailbox;
- every other CASCADE child table (``tags`` / ``telegram_*`` / ``sent_*`` /
  ``users_settings`` / ``user_groups`` …) was dropped in Phase D;
- ``groups.leader_user_id RESTRICT`` was removed with ``groups`` in Phase E.

The technical ``crm-service`` user (super_admin, group_id already gone) is
NEVER deleted — it owns every mailbox.

``downgrade()`` is an explicit, loud no-op guard: deleted human ``users`` rows
are data, not schema, and cannot be recreated by DDL. Recovery is by restoring
the §6 pg_dump backup (which snapshots ``users`` before this reduction), not by
``alembic downgrade``. See Phase D for the full rationale.

Revision ID: 20260715_028
Revises: 20260715_027
Create Date: 2026-07-15
"""

from __future__ import annotations

from alembic import op

revision = "20260715_028"
down_revision = "20260715_027"
branch_labels = None
depends_on = None

_CRM_SERVICE_USERNAME = "crm-service"


def upgrade() -> None:
    op.execute("DELETE FROM users " f"WHERE username <> '{_CRM_SERVICE_USERNAME}'")


def downgrade() -> None:
    raise RuntimeError(
        "ADR-0044 Phase F (deletion of human 'users' rows) is an irreversible "
        "decommission step (point of no return, ADR §4). The deleted rows are "
        "data, not schema, and cannot be recreated by DDL. Recovery is by "
        "restoring the §6 pg_dump backup (users snapshot), NOT by "
        "`alembic downgrade`."
    )
