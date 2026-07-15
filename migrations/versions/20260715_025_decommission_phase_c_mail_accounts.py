"""ADR-0044 Phase C — reduce ``mail_accounts`` schema for the CRM connector.

Runbook: ``docs/adr/ADR-0044-decommission-runbook.md`` §3 / §3.1 / §4 (Phase C).

The migration is **self-sufficient** (§3.1): it idempotently seeds the technical
``crm-service`` owner ITSELF, right before repointing mailboxes onto it, instead
of relying on the app-lifespan ``seed_crm_service_user`` having run. Migrations
apply in contexts where the app has not (yet) started — CI runs ``alembic
upgrade head`` on an empty DB before booting the app (``ci.yml``), and a
restore/new-instance applies the schema before the first app boot. Depending on
"the app once seeded this row" is hidden coupling; self-seeding removes it so the
migration behaves identically on prod, in CI and on restore.

Operations, in this order (all safe now that the A-phase code detach is
deployed — the ORM no longer maps ``group_id`` and every account owner is the
technical ``crm-service`` user, ADR-0043 §4):

0. **Self-seed owner (§3.1).** ``INSERT INTO users (...) ... ON CONFLICT
   (username) DO NOTHING`` — idempotent raw SQL (NOT the ORM/``seed_crm_service_user``,
   the migration does not raise the app graph). On prod the row already exists
   (no-op); on an empty DB it is created so the repoint below can resolve it.
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

   The physical index ``ix_mail_accounts_group_id`` DOES exist in the live
   schema (created by migration ``20260509_009``) and is dropped here in Phase C
   (ADR §3). The reduced ORM ``__table_args__`` in ``shared/models/mail_account.py``
   no longer lists it (only ``ix_mail_accounts_user_id`` /
   ``ix_mail_accounts_active_partial`` remain), so reading the ORM is misleading
   — the DDL, not the ORM, is authoritative for the live schema.

``downgrade()`` structurally restores the ``group_id`` column + FK + index
(matching migration ``20260509_009``). The *data* (original owners / original
``group_id`` values) is NOT restored — repointing is irreversible by design
(ADR §3). ``downgrade()`` does NOT delete the self-seeded ``crm-service`` row —
it is the KEEP owner of every mailbox (deleting it would violate
``mail_accounts.user_id NOT NULL``); the §3.1 self-seed is by design not undone.
Downgrading this revision requires ``groups`` to still exist, which holds while
sitting between Phase C and Phase E.

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

# ADR-0044 §3.1 — idempotent self-seed of the technical mailbox owner.
# Raw alembic-level SQL (NOT the ORM ``seed_crm_service_user``: the migration
# does not raise the app graph). Fields verified against ``seed_crm_service_user``
# (backend/app/auth/service.py) and the ``users`` schema at Phase C:
#   - username='crm-service' — NOT NULL, lowercase (CHECK ck_users_username_lower);
#     ON CONFLICT (username) relies on uq_users_username.
#   - role='super_admin'     — CHECK ck_users_role; technical owner must be super_admin.
#   - password_reset_required=false — matches the seed (DB default is true).
# Other NOT NULL columns take their server-defaults (id, failed_login_attempts,
# created_at/updated_at); nullable columns (email, display_name, password_hash,
# password_encrypted, group_id) default to NULL.
_SEED_CRM_SERVICE_SQL = sa.text(
    """
    INSERT INTO users (username, role, password_reset_required)
    VALUES (:username, 'super_admin', false)
    ON CONFLICT (username) DO NOTHING
    """
)


def _crm_service_id() -> int:
    """Resolve the technical mailbox-owner id, self-seeding it if missing.

    ADR-0044 §3.1: seed-if-missing → SELECT. The migration must not depend on the
    app-lifespan seed having run, so it idempotently inserts ``crm-service`` here
    (no-op if it already exists) and then reads its id back. The ``RuntimeError``
    below is a defensive invariant only — after the self-seed the row must exist,
    so it is theoretically unreachable (belt-and-suspenders).
    """
    bind = op.get_bind()
    bind.execute(_SEED_CRM_SERVICE_SQL, {"username": _CRM_SERVICE_USERNAME})
    row = bind.execute(
        sa.text("SELECT id FROM users WHERE username = :u"),
        {"u": _CRM_SERVICE_USERNAME},
    ).fetchone()
    if row is None:
        raise RuntimeError(
            "ADR-0044 Phase C: technical user 'crm-service' not found in 'users' "
            "even after the self-seed INSERT. This defensive invariant should be "
            "unreachable — investigate the users table before repointing mailboxes."
        )
    return int(row[0])


def upgrade() -> None:
    # 0) Self-seed the technical owner BEFORE repointing (ADR-0044 §3.1) so the
    #    migration is self-sufficient on an empty DB (CI / restore) and does not
    #    depend on the app-lifespan seed having run.
    crm_id = _crm_service_id()

    # 1) Repoint every mailbox still owned by a human user onto crm-service.
    op.execute(
        sa.text("UPDATE mail_accounts SET user_id = :crm_id WHERE user_id <> :crm_id").bindparams(
            crm_id=crm_id
        )
    )

    # 1b) Flush deferred FK trigger events queued by the self-seed INSERT / repoint
    #     above. ``users_group_id_fkey`` (users.group_id → groups) is DEFERRABLE
    #     INITIALLY DEFERRED, so inserting the crm-service row queues an un-fired
    #     deferred check on ``users``. alembic runs the whole decommission chain in
    #     ONE transaction (no ``transaction_per_migration``), so that pending event
    #     would survive into Phase E (``20260715_027``) and make its
    #     ``ALTER TABLE users DROP COLUMN group_id`` fail with
    #     "cannot ALTER TABLE users because it has pending trigger events".
    #     crm-service.group_id IS NULL (and every repointed mailbox now points at a
    #     valid owner), so forcing the deferred checks now is trivially satisfied
    #     and clears the queue for the later DDL migrations.
    op.execute("SET CONSTRAINTS ALL IMMEDIATE")

    # 2) Drop group_id (FK + index go with the column; index dropped explicitly
    #    for symmetry with downgrade()).
    op.execute("DROP INDEX IF EXISTS ix_mail_accounts_group_id")
    op.execute("ALTER TABLE mail_accounts DROP COLUMN group_id")


def downgrade() -> None:
    # Structural restore only — original owners / group_id values are gone, and
    # the self-seeded crm-service row is intentionally kept (owner of every
    # mailbox). Requires 'groups' to exist (true between Phase C and Phase E).
    op.execute(
        "ALTER TABLE mail_accounts "
        "ADD COLUMN group_id BIGINT NULL "
        "REFERENCES groups(id) ON DELETE SET NULL"
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_mail_accounts_group_id ON mail_accounts (group_id)")
