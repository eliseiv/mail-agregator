"""Outbound webhooks for teams (ADR-0023).

Creates two new tables and reuses the shared ``set_updated_at`` trigger
function:

- ``webhooks``            — one configuration row per group (group_leader
  manages it via ``/api/webhooks/me``). ``secret_encrypted`` is AES-256-GCM
  with AAD bound to the ``webhook_id`` (see ``shared/crypto.py``).
- ``webhook_deliveries``  — idempotency registry for delivered events:
  ``UNIQUE(webhook_id, message_id)`` guarantees the same message is never
  POST'ed twice to the same webhook (ADR-0023 §3.4).

``admin_audit.action`` is free-form TEXT at the DB level — no DDL is
needed for the five new action values
(``webhook_created``, ``webhook_updated``, ``webhook_deleted``,
``webhook_secret_rotated``, ``webhook_dead_marked``); the closed set is
enforced in :mod:`backend.app.audit.service`.

Revision ID: 20260515_014
Revises: 20260514_013
Create Date: 2026-05-15
"""

from __future__ import annotations

from alembic import op

revision = "20260515_014"
down_revision = "20260514_013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---- webhooks --------------------------------------------------------
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS webhooks (
            id                   BIGSERIAL PRIMARY KEY,
            group_id             BIGINT NOT NULL UNIQUE
                                 REFERENCES groups(id) ON DELETE CASCADE,
            url                  TEXT NOT NULL,
            secret_encrypted     BYTEA NOT NULL,
            is_active            BOOLEAN NOT NULL DEFAULT TRUE,
            consecutive_failures INT NOT NULL DEFAULT 0,
            dead_at              TIMESTAMPTZ NULL,
            last_fired_at        TIMESTAMPTZ NULL,
            last_error           TEXT NULL,
            created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT webhooks_url_https_check
                CHECK (url LIKE 'https://%'),
            CONSTRAINT webhooks_url_length_check
                CHECK (char_length(url) BETWEEN 9 AND 2048)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS webhooks_active_idx "
        "ON webhooks(is_active) WHERE is_active = TRUE"
    )
    # ``set_updated_at()`` is created by 20260505_001_initial_schema.
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_trigger WHERE tgname = 'trg_webhooks_updated_at'
            ) THEN
                CREATE TRIGGER trg_webhooks_updated_at
                BEFORE UPDATE ON webhooks
                FOR EACH ROW EXECUTE FUNCTION set_updated_at();
            END IF;
        END;
        $$
        """
    )

    # ---- webhook_deliveries ---------------------------------------------
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS webhook_deliveries (
            id               BIGSERIAL PRIMARY KEY,
            webhook_id       BIGINT NOT NULL
                             REFERENCES webhooks(id) ON DELETE CASCADE,
            message_id       BIGINT NOT NULL
                             REFERENCES messages(id) ON DELETE CASCADE,
            sent_at          TIMESTAMPTZ NULL,
            response_code    INT NULL,
            response_excerpt TEXT NULL,
            created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT webhook_deliveries_unique
                UNIQUE (webhook_id, message_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS webhook_deliveries_webhook_id_idx "
        "ON webhook_deliveries(webhook_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS webhook_deliveries_message_id_idx "
        "ON webhook_deliveries(message_id)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS webhook_deliveries")
    op.execute("DROP TRIGGER IF EXISTS trg_webhooks_updated_at ON webhooks")
    op.execute("DROP TABLE IF EXISTS webhooks")
