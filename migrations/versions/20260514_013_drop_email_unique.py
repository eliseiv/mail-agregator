"""Drop the global UNIQUE INDEX ``ux_mail_accounts_email_lower`` (round-17 mistake).

Round-17 added a global UNIQUE constraint to prevent two teams from adding
the same mailbox — that turned out to be wrong UX. Teams *should* be
allowed to add the same email independently (each with its own
credentials). Duplicates are hidden at read-time by
``MailAccountsRepo.list_canonical_account_ids`` / dedup in
``MailAccountService.list_for_scope``.

Revision ID: 20260514_013
Revises: 20260514_012
Create Date: 2026-05-14
"""

from __future__ import annotations

from alembic import op

revision = "20260514_013"
down_revision = "20260514_012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ux_mail_accounts_email_lower")


def downgrade() -> None:
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_mail_accounts_email_lower "
        "ON mail_accounts (LOWER(email))"
    )
