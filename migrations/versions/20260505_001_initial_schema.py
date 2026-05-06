"""Initial schema — all 7 tables + indexes + updated_at triggers.

Mirrors ``docs/03-data-model.md`` exactly.

Revision ID: 20260505_001
Revises:
Create Date: 2026-05-05 00:00:00 UTC
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# Revision identifiers, used by Alembic.
revision: str = "20260505_001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_UPDATED_AT_FN = """
CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

_DROP_UPDATED_AT_FN = "DROP FUNCTION IF EXISTS set_updated_at();"


def upgrade() -> None:
    # ---- updated_at trigger function ------------------------------------
    op.execute(_UPDATED_AT_FN)

    # ---- users ----------------------------------------------------------
    op.create_table(
        "users",
        sa.Column(
            "id",
            sa.BigInteger(),
            primary_key=True,
            autoincrement=True,
            nullable=False,
        ),
        sa.Column("username", sa.Text(), nullable=False),
        sa.Column("email", sa.Text(), nullable=True),
        sa.Column("password_hash", sa.String(length=255), nullable=True),
        sa.Column(
            "is_admin",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "password_reset_required",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column("lockout_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "failed_login_attempts",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("username", name="uq_users_username"),
    )
    op.create_index(
        "ix_users_is_admin_partial",
        "users",
        ["is_admin"],
        postgresql_where=sa.text("is_admin = true"),
    )
    op.execute(
        "CREATE TRIGGER trg_users_updated_at "
        "BEFORE UPDATE ON users "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at();"
    )

    # ---- mail_accounts --------------------------------------------------
    op.create_table(
        "mail_accounts",
        sa.Column(
            "id",
            sa.BigInteger(),
            primary_key=True,
            autoincrement=True,
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("encrypted_password", sa.LargeBinary(), nullable=False),
        sa.Column("imap_host", sa.Text(), nullable=False),
        sa.Column(
            "imap_port",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("993"),
        ),
        sa.Column(
            "imap_ssl",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column("smtp_host", sa.Text(), nullable=False),
        sa.Column(
            "smtp_port",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("465"),
        ),
        sa.Column(
            "smtp_ssl",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "smtp_starttls",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("smtp_username", sa.Text(), nullable=True),
        sa.Column("smtp_encrypted_password", sa.LargeBinary(), nullable=True),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column("last_synced_uidnext", sa.BigInteger(), nullable=True),
        sa.Column("last_uidvalidity", sa.BigInteger(), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_sync_error", sa.Text(), nullable=True),
        sa.Column(
            "consecutive_failures",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "imap_port BETWEEN 1 AND 65535", name="ck_mail_accounts_imap_port"
        ),
        sa.CheckConstraint(
            "smtp_port BETWEEN 1 AND 65535", name="ck_mail_accounts_smtp_port"
        ),
        sa.CheckConstraint(
            "NOT (smtp_ssl AND smtp_starttls)",
            name="ck_mail_accounts_smtp_ssl_xor",
        ),
        sa.UniqueConstraint(
            "user_id", "email", name="uq_mail_accounts_user_email"
        ),
    )
    op.create_index(
        "ix_mail_accounts_user_id", "mail_accounts", ["user_id"]
    )
    op.create_index(
        "ix_mail_accounts_active_partial",
        "mail_accounts",
        ["is_active"],
        postgresql_where=sa.text("is_active = true"),
    )
    op.execute(
        "CREATE TRIGGER trg_mail_accounts_updated_at "
        "BEFORE UPDATE ON mail_accounts "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at();"
    )

    # ---- messages -------------------------------------------------------
    op.create_table(
        "messages",
        sa.Column(
            "id",
            sa.BigInteger(),
            primary_key=True,
            autoincrement=True,
            nullable=False,
        ),
        sa.Column(
            "mail_account_id",
            sa.BigInteger(),
            sa.ForeignKey("mail_accounts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("uid", sa.BigInteger(), nullable=False),
        sa.Column("uidvalidity", sa.BigInteger(), nullable=False),
        sa.Column("message_id_header", sa.Text(), nullable=True),
        sa.Column("from_addr", sa.Text(), nullable=False),
        sa.Column("from_name", sa.Text(), nullable=True),
        sa.Column(
            "to_addrs",
            sa.Text(),
            nullable=False,
            server_default=sa.text("''"),
        ),
        sa.Column("cc_addrs", sa.Text(), nullable=True),
        sa.Column("subject", sa.Text(), nullable=True),
        sa.Column("internal_date", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "body_text",
            sa.Text(),
            nullable=False,
            server_default=sa.text("''"),
        ),
        sa.Column(
            "body_truncated",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "body_present",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "is_read",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("in_reply_to", sa.Text(), nullable=True),
        sa.Column("refs_header", sa.Text(), nullable=True),
        sa.Column(
            "fetched_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "mail_account_id",
            "uidvalidity",
            "uid",
            name="uq_messages_account_uidv_uid",
        ),
    )
    op.execute(
        "CREATE INDEX ix_messages_account_internal_date_desc "
        "ON messages (mail_account_id, internal_date DESC)"
    )
    op.create_index(
        "ix_messages_unread_partial",
        "messages",
        ["mail_account_id", "is_read"],
        postgresql_where=sa.text("is_read = false"),
    )
    op.create_index(
        "ix_messages_internal_date", "messages", ["internal_date"]
    )

    # ---- attachments ----------------------------------------------------
    op.create_table(
        "attachments",
        sa.Column(
            "id",
            sa.BigInteger(),
            primary_key=True,
            autoincrement=True,
            nullable=False,
        ),
        sa.Column(
            "message_id",
            sa.BigInteger(),
            sa.ForeignKey("messages.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("content_type", sa.Text(), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("s3_key", sa.Text(), nullable=False),
        sa.Column(
            "skipped_too_large",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_attachments_message_id", "attachments", ["message_id"]
    )

    # ---- sent_messages --------------------------------------------------
    op.create_table(
        "sent_messages",
        sa.Column(
            "id",
            sa.BigInteger(),
            primary_key=True,
            autoincrement=True,
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "from_account_id",
            sa.BigInteger(),
            sa.ForeignKey("mail_accounts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("to_addrs", sa.Text(), nullable=False),
        sa.Column("cc_addrs", sa.Text(), nullable=True),
        sa.Column("bcc_addrs", sa.Text(), nullable=True),
        sa.Column("subject", sa.Text(), nullable=True),
        sa.Column("body_text", sa.Text(), nullable=False),
        sa.Column("in_reply_to", sa.Text(), nullable=True),
        sa.Column("refs_header", sa.Text(), nullable=True),
        sa.Column("smtp_message_id", sa.Text(), nullable=False),
        sa.Column(
            "appended_to_sent",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("appended_error", sa.Text(), nullable=True),
        sa.Column(
            "sent_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.execute(
        "CREATE INDEX ix_sent_messages_user_sent_at_desc "
        "ON sent_messages (user_id, sent_at DESC)"
    )
    op.create_index(
        "ix_sent_messages_from_account", "sent_messages", ["from_account_id"]
    )

    # ---- sent_attachments (placeholder, see ADR-0011 / TD-005) ----------
    op.create_table(
        "sent_attachments",
        sa.Column(
            "id",
            sa.BigInteger(),
            primary_key=True,
            autoincrement=True,
            nullable=False,
        ),
        sa.Column(
            "sent_message_id",
            sa.BigInteger(),
            sa.ForeignKey("sent_messages.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("content_type", sa.Text(), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("s3_key", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    # ---- admin_audit (no FKs by design) ---------------------------------
    op.create_table(
        "admin_audit",
        sa.Column(
            "id",
            sa.BigInteger(),
            primary_key=True,
            autoincrement=True,
            nullable=False,
        ),
        sa.Column("actor_user_id", sa.BigInteger(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("target_user_id", sa.BigInteger(), nullable=True),
        sa.Column("target_username", sa.Text(), nullable=True),
        sa.Column("details", postgresql.JSONB(), nullable=True),
        sa.Column("ip", sa.Text(), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.execute(
        "CREATE INDEX ix_admin_audit_created_at_desc "
        "ON admin_audit (created_at DESC)"
    )
    op.execute(
        "CREATE INDEX ix_admin_audit_actor_created_desc "
        "ON admin_audit (actor_user_id, created_at DESC)"
    )
    op.create_index(
        "ix_admin_audit_target_user_partial",
        "admin_audit",
        ["target_user_id"],
        postgresql_where=sa.text("target_user_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_table("admin_audit")
    op.drop_table("sent_attachments")
    op.drop_table("sent_messages")
    op.drop_table("attachments")
    op.drop_table("messages")
    op.execute("DROP TRIGGER IF EXISTS trg_mail_accounts_updated_at ON mail_accounts")
    op.drop_table("mail_accounts")
    op.execute("DROP TRIGGER IF EXISTS trg_users_updated_at ON users")
    op.drop_table("users")
    op.execute(_DROP_UPDATED_AT_FN)
