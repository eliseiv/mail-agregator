"""Telegram persistent SSO + push-notifications (ADR-0022).

Creates three new tables:

- ``telegram_links``        — link Telegram User.id ↔ internal users.id.
- ``telegram_notifications`` — registry of delivered push notifications
  (dedup by ``(message_id, user_id)``).
- ``users_settings``        — per-user preferences (``tg_notifications_enabled``
  on this iteration; extensible later).

``admin_audit.action`` is a free-form TEXT column without a database enum
constraint (see ``backend/app/audit/service.py:ALLOWED_ACTIONS`` for the
app-level closed set); no DDL is needed for the four new action values
(``telegram_link_created``, ``telegram_link_revoked``,
``telegram_link_dead_marked``, ``telegram_link_collision``).

Revision ID: 20260510_010
Revises: 20260509_009
Create Date: 2026-05-10
"""

from __future__ import annotations

from alembic import op

revision = "20260510_010"
down_revision = "20260509_009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---- telegram_links --------------------------------------------------
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS telegram_links (
            telegram_user_id BIGINT PRIMARY KEY,
            user_id          BIGINT NOT NULL UNIQUE
                             REFERENCES users(id) ON DELETE CASCADE,
            created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
            dead_at          TIMESTAMPTZ NULL
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS telegram_links_user_id_idx "
        "ON telegram_links(user_id)"
    )

    # ---- telegram_notifications -----------------------------------------
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS telegram_notifications (
            id                  BIGSERIAL PRIMARY KEY,
            message_id          BIGINT NOT NULL
                                REFERENCES messages(id) ON DELETE CASCADE,
            user_id             BIGINT NOT NULL
                                REFERENCES users(id) ON DELETE CASCADE,
            sent_at             TIMESTAMPTZ NULL,
            telegram_message_id BIGINT NULL,
            CONSTRAINT telegram_notifications_unique
                UNIQUE (message_id, user_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS telegram_notifications_message_id_idx "
        "ON telegram_notifications(message_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS telegram_notifications_user_id_idx "
        "ON telegram_notifications(user_id)"
    )

    # ---- users_settings --------------------------------------------------
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS users_settings (
            user_id                  BIGINT PRIMARY KEY
                                     REFERENCES users(id) ON DELETE CASCADE,
            tg_notifications_enabled BOOLEAN NOT NULL DEFAULT TRUE,
            updated_at               TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    # Re-use the shared ``set_updated_at`` trigger function created in the
    # initial migration (``20260505_001_initial_schema.py``).
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_trigger
                WHERE tgname = 'trg_users_settings_updated_at'
            ) THEN
                CREATE TRIGGER trg_users_settings_updated_at
                BEFORE UPDATE ON users_settings
                FOR EACH ROW EXECUTE FUNCTION set_updated_at();
            END IF;
        END;
        $$
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS users_settings")
    op.execute("DROP TABLE IF EXISTS telegram_notifications")
    op.execute("DROP TABLE IF EXISTS telegram_links")
