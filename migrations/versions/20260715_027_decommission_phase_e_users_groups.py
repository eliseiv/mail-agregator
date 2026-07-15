"""ADR-0044 Phase E — untie ``users`` from ``groups`` and drop ``groups``.

Runbook: ``docs/adr/ADR-0044-decommission-runbook.md`` §4 (Phase E).

Two operations, in this order:

1. ``ALTER TABLE users DROP COLUMN group_id`` — this automatically removes the
   dependent FK ``users_group_id_fkey`` (→ ``groups`` ON DELETE SET NULL,
   DEFERRABLE INITIALLY DEFERRED) and the partial index
   ``ix_users_group_id_partial`` (``WHERE group_id IS NOT NULL``). The ORM
   mapping for ``User.group_id`` / ``User.group`` was already removed in the
   A-phase detach. ``crm-service`` (super_admin, group_id NULL) satisfies every
   remaining invariant.

   NOTE (divergence from ADR §4 wording): ADR §4 Phase E says this DROP COLUMN
   also removes CHECK ``users_role_group_invariant``. That constraint does NOT
   exist in the live schema — it was dropped historically (migration
   ``20260508_005_drop_role_group_check``). Nothing to do; ``DROP COLUMN``
   removes exactly what still depends on the column (the FK + partial index).
   Flagged for architect.

2. ``DROP TABLE groups`` — by now nothing references it: ``mail_accounts.group_id``
   (Phase C) and ``users.group_id`` (step 1) are gone; ``user_groups`` /
   ``group_forwarding`` / ``message_forwards`` / ``webhooks`` were dropped in
   Phase D; ``groups.leader_user_id RESTRICT → users`` leaves with the table.

``downgrade()`` is an explicit, loud no-op guard: Phase E is past the ADR §4
point of no return (``groups`` data and every human ``users.group_id`` value
are gone). Recovery is by restoring the §6 pg_dump backup, not by
``alembic downgrade``. See Phase D for the full rationale.

Revision ID: 20260715_027
Revises: 20260715_026
Create Date: 2026-07-15
"""

from __future__ import annotations

from alembic import op

revision = "20260715_027"
down_revision = "20260715_026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # DROP COLUMN cascades to users_group_id_fkey + ix_users_group_id_partial.
    op.execute("ALTER TABLE users DROP COLUMN group_id")
    op.execute("DROP TABLE groups")


def downgrade() -> None:
    raise RuntimeError(
        "ADR-0044 Phase E (drop of 'groups' + removal of users.group_id) is an "
        "irreversible decommission step (point of no return, ADR §4). The "
        "'groups' data and human users.group_id values are permanently gone. "
        "Recovery is by restoring the §6 pg_dump backup, NOT by "
        "`alembic downgrade`. Refusing to fabricate an empty structural shell "
        "that would falsely imply reversibility."
    )
