"""Add ``user_groups`` M:N membership table (ADR-0030 multi-group).

Introduces the additive join-table ``user_groups`` as the source of truth
for mailbox/message visibility, Telegram-notification addressing and team
member counts. ``users.group_id`` is kept as the "home"/primary team and is
NOT dropped; ``mail_accounts.group_id`` is NOT touched.

Backfill: for every ``users.group_id IS NOT NULL`` we insert the matching
``user_groups(user_id, group_id)`` row so the home membership is always
mirrored (ADR-0030 invariant). ``ON CONFLICT DO NOTHING`` keeps it
idempotent if re-run.

Revision ID: 20260623_019
Revises: 20260527_018
Create Date: 2026-06-23
"""

from __future__ import annotations

from alembic import op

revision = "20260623_019"
down_revision = "20260527_018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1) Create the join-table. BIGSERIAL PK to match the rest of the schema.
    op.execute(
        """
        CREATE TABLE user_groups (
            id         BIGSERIAL    PRIMARY KEY,
            user_id    BIGINT       NOT NULL REFERENCES users(id)  ON DELETE CASCADE,
            group_id   BIGINT       NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
            created_at TIMESTAMPTZ  NOT NULL DEFAULT now(),
            CONSTRAINT uq_user_groups_user_group UNIQUE (user_id, group_id)
        )
        """
    )
    # 2) Reverse-lookup index "members of a team".
    op.execute("CREATE INDEX ix_user_groups_group_id ON user_groups (group_id)")
    # 3) Backfill home memberships from users.group_id.
    op.execute(
        """
        INSERT INTO user_groups (user_id, group_id)
        SELECT u.id, u.group_id
        FROM   users u
        WHERE  u.group_id IS NOT NULL
        ON CONFLICT (user_id, group_id) DO NOTHING
        """
    )


def downgrade() -> None:
    # ``users.group_id`` was never removed, so dropping the table fully
    # reverts the revision.
    op.execute("DROP TABLE IF EXISTS user_groups")
