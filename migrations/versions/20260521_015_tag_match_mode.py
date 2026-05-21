"""Per-tag rule match mode: 'any' (OR) or 'all' (AND).

Adds ``tags.match_mode`` so each tag can declare whether it attaches when
**any** of its rules match (the existing OR semantics, the default that
preserves backward-compat for every existing tag) or only when **all** of
its rules match (AND semantics).

Existing rows get ``'any'`` via the column default, so behaviour is
unchanged on upgrade. A CHECK constraint mirrors the
``Literal['any','all']`` enforced at the API layer
(``backend/app/tags/schemas.py``) — defence-in-depth so a direct DB write
cannot smuggle an invalid mode that the SQL in ``backend/app/tags/sql.py``
would silently ignore.

Idempotent: ``ADD COLUMN IF NOT EXISTS`` plus a guarded ``ADD CONSTRAINT``
so re-running the migration (or running it on a partially-migrated DB) is
safe.

Revision ID: 20260521_015
Revises: 20260515_014
Create Date: 2026-05-21
"""

from __future__ import annotations

from alembic import op

revision = "20260521_015"
down_revision = "20260515_014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE tags "
        "ADD COLUMN IF NOT EXISTS match_mode TEXT NOT NULL DEFAULT 'any'"
    )
    # ``ADD CONSTRAINT`` has no ``IF NOT EXISTS`` form before PG 16, so guard
    # it explicitly to keep the migration idempotent across versions.
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'ck_tags_match_mode'
            ) THEN
                ALTER TABLE tags
                ADD CONSTRAINT ck_tags_match_mode
                CHECK (match_mode IN ('any', 'all'));
            END IF;
        END;
        $$
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE tags DROP CONSTRAINT IF EXISTS ck_tags_match_mode")
    op.execute("ALTER TABLE tags DROP COLUMN IF EXISTS match_mode")
