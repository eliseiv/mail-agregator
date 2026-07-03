"""Mail forwarding for teams (ADR-0034).

Creates two new tables and reuses the shared ``set_updated_at`` trigger
function (created by ``20260505_001_initial_schema``):

- ``group_forwarding``  — one configuration row per group (the leader's
  forward-to address + ``is_active`` toggle). Fork of ``webhooks`` WITHOUT
  a secret — forwarding sends through the mailbox's own SMTP credentials.
- ``message_forwards``  — idempotency/claim registry for forwarded events:
  ``UNIQUE(message_id, group_id)`` guarantees a message is forwarded to a
  team's address exactly once (ADR-0034 §1.2), even on queue duplicates /
  worker restart (claim ``INSERT ... ON CONFLICT DO NOTHING RETURNING id``).

``admin_audit.action`` is free-form TEXT at the DB level — no DDL is needed
for the two new action values (``forwarding_updated`` / ``forwarding_deleted``);
the closed set is enforced in :mod:`backend.app.audit.service`.

Forward-only per ``07-deployment.md`` migration policy. ``down`` drops both
tables (non-lossy — both are operational; the forwarding config is restored
by the leader via the UI).

Revision ID: 20260703_021
Revises: 20260703_020
Create Date: 2026-07-03
"""

from __future__ import annotations

from alembic import op

revision = "20260703_021"
down_revision = "20260703_020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---- group_forwarding ------------------------------------------------
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS group_forwarding (
            id          BIGSERIAL PRIMARY KEY,
            group_id    BIGINT NOT NULL UNIQUE
                        REFERENCES groups(id) ON DELETE CASCADE,
            forward_to  TEXT NOT NULL,
            is_active   BOOLEAN NOT NULL DEFAULT TRUE,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    # ``set_updated_at()`` is created by 20260505_001_initial_schema.
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_trigger WHERE tgname = 'trg_group_forwarding_updated_at'
            ) THEN
                CREATE TRIGGER trg_group_forwarding_updated_at
                BEFORE UPDATE ON group_forwarding
                FOR EACH ROW EXECUTE FUNCTION set_updated_at();
            END IF;
        END;
        $$
        """
    )

    # ---- message_forwards ------------------------------------------------
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS message_forwards (
            id          BIGSERIAL PRIMARY KEY,
            message_id  BIGINT NOT NULL
                        REFERENCES messages(id) ON DELETE CASCADE,
            group_id    BIGINT NOT NULL
                        REFERENCES groups(id) ON DELETE CASCADE,
            forward_to  TEXT NOT NULL,
            sent_at     TIMESTAMPTZ NULL,
            error       TEXT NULL,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT message_forwards_unique UNIQUE (message_id, group_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS message_forwards_message_id_idx "
        "ON message_forwards(message_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS message_forwards_group_id_idx ON message_forwards(group_id)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS message_forwards")
    op.execute("DROP TRIGGER IF EXISTS trg_group_forwarding_updated_at ON group_forwarding")
    op.execute("DROP TABLE IF EXISTS group_forwarding")
