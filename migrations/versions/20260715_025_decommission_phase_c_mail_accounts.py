"""ADR-0044 Phase C — reduce ``mail_accounts`` schema for the CRM connector.

Runbook: ``docs/adr/ADR-0044-decommission-runbook.md`` §3 / §4 (Phase C).

Two operations, in this order (both safe now that the A-phase code detach is
deployed — the ORM no longer maps ``group_id`` and every account owner is the
technical ``crm-service`` user, ADR-0043 §4):

1. **Data — repoint owners.** ``UPDATE mail_accounts SET user_id =
   <crm-service.id>`` for every account still owned by a human user. After the
   human ``users`` rows are deleted (Phase F) the ``mail_accounts.user_id NOT
   NULL CASCADE`` FK must NOT cascade-delete any mailbox — repointing them onto
   the surviving ``crm-service`` row guarantees that.
2. **Schema — drop ``mail_accounts.group_id``.** Mailbox-to-team ownership
   lives in the CRM only. ``DROP COLUMN`` automatically removes the dependent
   FK ``mail_accounts_group_id_fkey`` (→ ``groups`` ON DELETE SET NULL) and the
   physical index ``ix_mail_accounts_group_id``; the index is dropped
   explicitly first for symmetry with ``downgrade()``.

   NOTE (divergence from ADR §3 wording): the physical index
   ``ix_mail_accounts_group_id`` DOES exist in the live schema (created by
   migration ``20260509_009``). ADR §3 states "индекса по group_id нет" because
   it read the *reduced* ORM ``__table_args__`` (from which ``group_id`` and its
   index were already removed in the lock-step code detach). The DDL is correct
   regardless — dropping the column removes the index — but the ADR sentence is
   stale and is flagged for architect.

``downgrade()`` structurally restores the ``group_id`` column + FK + index
(matching migration ``20260509_009``). The *data* (original owners / original
``group_id`` values) is NOT restored — repointing is irreversible by design
(ADR §3). Downgrading this revision requires ``groups`` to still exist, which
holds while sitting between Phase C and Phase E.

Revision ID: 20260715_025
Revises: 20260710_024
Create Date: 2026-07-15
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260715_025"
down_revision = "20260710_024"
branch_labels = None
depends_on = None

_CRM_SERVICE_USERNAME = "crm-service"


def _crm_service_id() -> int:
    """Resolve the surviving technical mailbox-owner id, or fail loudly."""
    bind = op.get_bind()
    row = bind.execute(
        sa.text("SELECT id FROM users WHERE username = :u"),
        {"u": _CRM_SERVICE_USERNAME},
    ).fetchone()
    if row is None:
        raise RuntimeError(
            "ADR-0044 Phase C: technical user 'crm-service' not found in "
            "'users'. Refusing to repoint mail_accounts onto a missing owner. "
            "Seed 'crm-service' (seed_crm_service_user, ADR-0039) first."
        )
    return int(row[0])


def upgrade() -> None:
    crm_id = _crm_service_id()

    # 1) Repoint every mailbox still owned by a human user onto crm-service.
    op.execute("UPDATE mail_accounts " f"SET user_id = {crm_id} " f"WHERE user_id <> {crm_id}")

    # 2) Drop group_id (FK + index go with the column; index dropped explicitly
    #    for symmetry with downgrade()).
    op.execute("DROP INDEX IF EXISTS ix_mail_accounts_group_id")
    op.execute("ALTER TABLE mail_accounts DROP COLUMN group_id")


def downgrade() -> None:
    # Structural restore only — original owners / group_id values are gone.
    # Requires 'groups' to exist (true between Phase C and Phase E).
    op.execute(
        "ALTER TABLE mail_accounts "
        "ADD COLUMN group_id BIGINT NULL "
        "REFERENCES groups(id) ON DELETE SET NULL"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_mail_accounts_group_id " "ON mail_accounts (group_id)"
    )
