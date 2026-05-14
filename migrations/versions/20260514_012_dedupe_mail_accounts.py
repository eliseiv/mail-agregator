"""Resolve historical email duplicates in mail_accounts (round-16 bug).

Pre round-16 the application only enforced ``UNIQUE (user_id, email)`` on
``mail_accounts`` — there was no global uniqueness. As a result the same
address (e.g. ``support@gmail.com``) could be added by two different users
(typically one per team). The worker's IMAP ``sync_cycle`` then polled each
row independently, fetched identical UIDs from the provider, and inserted
**duplicate** ``messages`` rows (one per ``mail_account_id``). Side-effects:

  * Inbox / dashboard showed every email twice for any caller who could see
    both mail accounts (super_admin, or a leader who happened to see both
    teams).
  * Auto-tagging fired twice — each duplicate message picked up the tag
    belonging to its team — surfacing as duplicate badges.
  * "Почты" page listed two rows with the same address.

This migration performs a one-shot cleanup and locks in the fix:

1. For each set of duplicates by ``LOWER(email)``, the **oldest** row
   (``MIN(id)``) is kept as the survivor and the others are deleted.
   **DATA LOSS warning**: messages belonging to the duplicate rows are
   dropped along with the rows (CASCADE on ``messages.mail_account_id``).
   This is acceptable because:
     - the surviving account will re-fetch the same messages from IMAP on
       the next worker tick (they live on the mail server, not in our DB);
     - the duplicates currently render to users as repeated noise, so
       discarding them is a net UX improvement.
2. A partial-free UNIQUE index on ``LOWER(email)`` is installed so the new
   application-level check in ``MailAccountService.create`` has database
   backing (defence-in-depth against race conditions).

Revision ID: 20260514_012
Revises: 20260513_011
Create Date: 2026-05-14
"""

from __future__ import annotations

from alembic import op

revision = "20260514_012"
down_revision = "20260513_011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1) Drop messages owned by duplicate mail_accounts.
    #    We pick the survivor per LOWER(email) as the row with the smallest
    #    id (= the oldest, first added). CASCADE FKs on messages handle the
    #    rest if we delete the mail_account row directly, but we delete
    #    messages explicitly first to keep the migration's intent obvious
    #    in audit logs and to avoid relying on CASCADE-deletion ordering.
    op.execute(
        """
        WITH dupes AS (
            SELECT id,
                   MIN(id) OVER (PARTITION BY LOWER(email)) AS survivor_id
            FROM mail_accounts
        )
        DELETE FROM messages
        WHERE mail_account_id IN (
            SELECT id FROM dupes WHERE id <> survivor_id
        );
        """
    )

    # 2) Drop the duplicate mail_account rows themselves.
    #    Any remaining child rows (attachments, sent_messages, etc.) cascade
    #    via existing FK ON DELETE CASCADE definitions from 20260505_001.
    op.execute(
        """
        WITH dupes AS (
            SELECT id,
                   MIN(id) OVER (PARTITION BY LOWER(email)) AS survivor_id
            FROM mail_accounts
        )
        DELETE FROM mail_accounts
        WHERE id IN (SELECT id FROM dupes WHERE id <> survivor_id);
        """
    )

    # 3) Lock in the invariant: case-insensitive global uniqueness of email.
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_mail_accounts_email_lower
        ON mail_accounts (LOWER(email));
        """
    )


def downgrade() -> None:
    # Only the index reversal is possible — the deleted duplicate rows
    # cannot be reconstructed. Worker will eventually re-sync messages for
    # the surviving accounts on the next IMAP poll.
    op.execute("DROP INDEX IF EXISTS ux_mail_accounts_email_lower")
