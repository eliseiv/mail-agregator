"""Global tags — nullable ``tags.user_id`` + partial-unique global name (ADR-0040).

ADR-0040 §1: a tag with ``user_id IS NULL`` is **global** (visible/applied to
every message of the system) — the single admin catalogue managed by the
headless CRM. Personal tags (``user_id NOT NULL``) stay for backward
compatibility.

Changes (additive, forward-only per ``07-deployment.md`` migration policy):

- ``ALTER COLUMN user_id DROP NOT NULL`` — allow the global ``NULL`` owner.
- ``CREATE UNIQUE INDEX uq_tags_global_name ON tags(name) WHERE user_id IS NULL``
  — global names are unique (a composite ``UNIQUE (user_id, name)`` does not
  constrain ``NULL`` rows, since ``NULL`` != ``NULL`` in Postgres).

Existing builtin/personal tag rows are NOT deleted — the global builtin
catalogue is seeded idempotently on startup (``seed_builtin_tags``, ADR-0040
§3); pre-existing personal builtin rows simply remain as harmless personal
tags.

Revision ID: 20260709_023
Revises: 20260706_022
Create Date: 2026-07-09
"""

from __future__ import annotations

from alembic import op

revision = "20260709_023"
down_revision = "20260706_022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE tags ALTER COLUMN user_id DROP NOT NULL")
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_tags_global_name "
        "ON tags (name) WHERE user_id IS NULL"
    )


def downgrade() -> None:
    # Reverting requires that no global (``user_id IS NULL``) rows remain,
    # otherwise ``SET NOT NULL`` fails. ADR-0040 §Consequences: the rollback
    # path is "restore NOT NULL after migrating globals back to personal"
    # (unlikely) — left as a best-effort drop of the partial index + SET NOT
    # NULL; if global rows exist the operator must remove/re-own them first.
    op.execute("DROP INDEX IF EXISTS uq_tags_global_name")
    op.execute("ALTER TABLE tags ALTER COLUMN user_id SET NOT NULL")
