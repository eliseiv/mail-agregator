"""Groups + roles + display_name (ADR-0019, ADR-0020).

Adds:

- ``groups`` table — one row per leader-led group.
- ``users.role TEXT`` (replaces ``users.is_admin``).
- ``users.display_name TEXT NULL``.
- ``users.group_id BIGINT NULL`` (FK to ``groups`` deferrable).
- ``users_role_group_invariant`` table CHECK (NOT VALID — left for super-admin
  to backfill via UI; see ADR-0019 §6 + ``docs/03-data-model.md`` "Миграции").
- ``users_group_leader_consistency_check`` constraint trigger (deferrable;
  defence-in-depth around the leader↔group invariant).
- ``mail_accounts.display_name TEXT NULL`` (ADR-0020).

Backfill rules:

- ``is_admin=true``  -> ``role='super_admin'``,    ``group_id IS NULL`` (OK).
- ``is_admin=false`` -> ``role='group_member'``,   ``group_id IS NULL``
  (violates the invariant). The invariant is therefore added with
  ``NOT VALID`` so existing rows are tolerated; new INSERT/UPDATE statements
  are still checked. Super-admin must redistribute legacy users via the new
  groups UI before running ``ALTER TABLE users VALIDATE CONSTRAINT
  users_role_group_invariant`` (post-deploy step, see release notes).

Revision ID: 20260508_004
Revises: 20260507_003
Create Date: 2026-05-08 00:00:00 UTC
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# Revision identifiers, used by Alembic.
revision: str = "20260508_004"
down_revision: Union[str, None] = "20260507_003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_LEADER_CONSISTENCY_FN = """
CREATE OR REPLACE FUNCTION check_group_leader_consistency()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.role = 'group_leader' THEN
        IF NOT EXISTS (
            SELECT 1 FROM groups g
            WHERE g.id = NEW.group_id AND g.leader_user_id = NEW.id
        ) THEN
            RAISE EXCEPTION USING
                ERRCODE = 'check_violation',
                MESSAGE = format(
                    'group_leader_consistency_violation: user %s role=group_leader but groups.leader_user_id != users.id for group_id=%s',
                    NEW.id, NEW.group_id
                );
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

_DROP_LEADER_CONSISTENCY_FN = "DROP FUNCTION IF EXISTS check_group_leader_consistency();"


def upgrade() -> None:
    # ---- 1. groups ------------------------------------------------------
    op.create_table(
        "groups",
        sa.Column(
            "id",
            sa.BigInteger(),
            primary_key=True,
            autoincrement=True,
            nullable=False,
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column(
            "leader_user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
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
        sa.UniqueConstraint("leader_user_id", name="uq_groups_leader_user_id"),
        sa.CheckConstraint(
            "char_length(name) BETWEEN 1 AND 100",
            name="ck_groups_name_length",
        ),
    )
    op.create_index("ix_groups_leader_user_id", "groups", ["leader_user_id"])
    op.execute(
        "CREATE TRIGGER trg_groups_updated_at "
        "BEFORE UPDATE ON groups "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at();"
    )

    # ---- 2. users: add nullable columns first --------------------------
    op.add_column(
        "users",
        sa.Column("role", sa.Text(), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column("display_name", sa.Text(), nullable=True),
    )
    # FK on group_id is DEFERRABLE INITIALLY DEFERRED so auto-create lead
    # flow (insert user, insert group with FK to user, update user.group_id)
    # can succeed in a single transaction.
    op.execute(
        "ALTER TABLE users ADD COLUMN group_id BIGINT NULL "
        "REFERENCES groups(id) ON DELETE SET NULL "
        "DEFERRABLE INITIALLY DEFERRED"
    )

    # ---- 3. data migration ---------------------------------------------
    # Map legacy boolean to new role enum.
    op.execute("UPDATE users SET role = 'super_admin' WHERE is_admin = true")
    op.execute("UPDATE users SET role = 'group_member' WHERE is_admin = false")

    # ---- 4. tighten role constraints -----------------------------------
    op.execute("ALTER TABLE users ALTER COLUMN role SET NOT NULL")
    op.execute("ALTER TABLE users ALTER COLUMN role SET DEFAULT 'group_member'")
    op.create_check_constraint(
        "ck_users_role",
        "users",
        "role IN ('super_admin', 'group_leader', 'group_member')",
    )
    op.create_check_constraint(
        "ck_users_display_name_length",
        "users",
        "display_name IS NULL OR char_length(display_name) BETWEEN 1 AND 100",
    )
    # NOT VALID — legacy non-admin users have no group_id yet; super-admin
    # must redistribute them via the new admin/groups UI before running
    # ``ALTER TABLE users VALIDATE CONSTRAINT users_role_group_invariant``
    # (post-deploy step). Until then the constraint is enforced only on
    # subsequent INSERT/UPDATE statements (Postgres standard semantics).
    op.execute(
        "ALTER TABLE users ADD CONSTRAINT users_role_group_invariant CHECK ("
        "(role = 'super_admin'  AND group_id IS NULL) OR "
        "(role = 'group_leader' AND group_id IS NOT NULL) OR "
        "(role = 'group_member' AND group_id IS NOT NULL)"
        ") NOT VALID"
    )

    # ---- 5. drop legacy column + index ---------------------------------
    op.drop_index("ix_users_is_admin_partial", table_name="users")
    op.drop_column("users", "is_admin")

    # ---- 6. new indexes -------------------------------------------------
    op.create_index(
        "ix_users_role_super_admin_partial",
        "users",
        ["role"],
        postgresql_where=sa.text("role = 'super_admin'"),
    )
    op.create_index(
        "ix_users_group_id_partial",
        "users",
        ["group_id"],
        postgresql_where=sa.text("group_id IS NOT NULL"),
    )

    # ---- 7. leader↔group consistency trigger (defence-in-depth) --------
    op.execute(_LEADER_CONSISTENCY_FN)
    op.execute(
        "CREATE CONSTRAINT TRIGGER trg_users_group_leader_consistency "
        "AFTER INSERT OR UPDATE OF role, group_id ON users "
        "DEFERRABLE INITIALLY DEFERRED "
        "FOR EACH ROW EXECUTE FUNCTION check_group_leader_consistency();"
    )

    # ---- 8. mail_accounts.display_name (ADR-0020) ----------------------
    op.add_column(
        "mail_accounts",
        sa.Column("display_name", sa.Text(), nullable=True),
    )
    op.create_check_constraint(
        "ck_mail_accounts_display_name_length",
        "mail_accounts",
        "display_name IS NULL OR char_length(display_name) BETWEEN 1 AND 100",
    )


def downgrade() -> None:
    # Reverse 8.
    op.drop_constraint(
        "ck_mail_accounts_display_name_length",
        "mail_accounts",
        type_="check",
    )
    op.drop_column("mail_accounts", "display_name")

    # Reverse 7.
    op.execute(
        "DROP TRIGGER IF EXISTS trg_users_group_leader_consistency ON users"
    )
    op.execute(_DROP_LEADER_CONSISTENCY_FN)

    # Reverse 6.
    op.drop_index("ix_users_group_id_partial", table_name="users")
    op.drop_index("ix_users_role_super_admin_partial", table_name="users")

    # Reverse 5: re-create is_admin column.
    op.add_column(
        "users",
        sa.Column(
            "is_admin",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.execute("UPDATE users SET is_admin = (role = 'super_admin')")
    op.create_index(
        "ix_users_is_admin_partial",
        "users",
        ["is_admin"],
        postgresql_where=sa.text("is_admin = true"),
    )

    # Reverse 4 + 3 + 2.
    op.drop_constraint(
        "users_role_group_invariant", "users", type_="check"
    )
    op.drop_constraint(
        "ck_users_display_name_length", "users", type_="check"
    )
    op.drop_constraint("ck_users_role", "users", type_="check")
    op.drop_column("users", "group_id")
    op.drop_column("users", "display_name")
    op.drop_column("users", "role")

    # Reverse 1.
    op.execute("DROP TRIGGER IF EXISTS trg_groups_updated_at ON groups")
    op.drop_index("ix_groups_leader_user_id", table_name="groups")
    op.drop_table("groups")
