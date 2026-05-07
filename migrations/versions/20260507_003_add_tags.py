"""Add tags / tag_rules / message_tags tables (ADR-0017).

Schema mirrors ``docs/03-data-model.md`` sections ``tags``, ``tag_rules``,
``message_tags``.

Builtin tags are NOT seeded here — they are created idempotently by the
auth post-login hook (see ADR-0017 §6 and ``docs/05-modules.md`` sec 17).

Revision ID: 20260507_003
Revises: 20260505_002
Create Date: 2026-05-07 00:00:00 UTC
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# Revision identifiers, used by Alembic.
revision: str = "20260507_003"
down_revision: Union[str, None] = "20260505_002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ---- tags -----------------------------------------------------------
    op.create_table(
        "tags",
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
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("color", sa.Text(), nullable=False),
        sa.Column(
            "is_builtin",
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
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("user_id", "name", name="uq_tags_user_name"),
        sa.CheckConstraint(
            "char_length(name) BETWEEN 1 AND 64",
            name="ck_tags_name_length",
        ),
        sa.CheckConstraint(
            r"color ~ '^#[0-9A-Fa-f]{6}$'",
            name="ck_tags_color_hex",
        ),
    )
    op.create_index("ix_tags_user_id", "tags", ["user_id"])
    op.execute(
        "CREATE TRIGGER trg_tags_updated_at "
        "BEFORE UPDATE ON tags "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at();"
    )

    # ---- tag_rules ------------------------------------------------------
    op.create_table(
        "tag_rules",
        sa.Column(
            "id",
            sa.BigInteger(),
            primary_key=True,
            autoincrement=True,
            nullable=False,
        ),
        sa.Column(
            "tag_id",
            sa.BigInteger(),
            sa.ForeignKey("tags.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("pattern", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "type IN ('subject_contains','body_contains','sender_contains','sender_exact')",
            name="ck_tag_rules_type",
        ),
        sa.CheckConstraint(
            "char_length(pattern) BETWEEN 1 AND 256",
            name="ck_tag_rules_pattern_length",
        ),
    )
    op.create_index("ix_tag_rules_tag_id", "tag_rules", ["tag_id"])

    # ---- message_tags ---------------------------------------------------
    op.create_table(
        "message_tags",
        sa.Column(
            "message_id",
            sa.BigInteger(),
            sa.ForeignKey("messages.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "tag_id",
            sa.BigInteger(),
            sa.ForeignKey("tags.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("message_id", "tag_id", name="pk_message_tags"),
    )
    op.create_index(
        "ix_message_tags_tag_message",
        "message_tags",
        ["tag_id", "message_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_message_tags_tag_message", table_name="message_tags")
    op.drop_table("message_tags")
    op.drop_index("ix_tag_rules_tag_id", table_name="tag_rules")
    op.drop_table("tag_rules")
    op.execute("DROP TRIGGER IF EXISTS trg_tags_updated_at ON tags")
    op.drop_index("ix_tags_user_id", table_name="tags")
    op.drop_table("tags")
