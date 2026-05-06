"""Defence-in-depth: enforce ``username = lower(username)`` at the DB layer.

App code (``backend.app.repositories.users.UsersRepo.create`` /
``get_by_username`` and the ``seed_super_admin`` flow) already lowercases
the username before INSERT/SELECT, so the existing UNIQUE(username) index
provides effective case-insensitive uniqueness *given* the app contract.
This CHECK constraint enforces that contract at the database level so a
hand-rolled SQL INSERT or a future code path that forgets to lowercase
cannot break the invariant and create two rows like ``admin`` / ``Admin``.

Backfill is implicit: the constraint is only added if all existing rows
already satisfy it. In a freshly-seeded database (or one created via the
existing app code) every row already has a lowercase username, so the
``ALTER TABLE ADD CONSTRAINT`` succeeds without any UPDATE.

Revision ID: 20260505_002
Revises: 20260505_001
Create Date: 2026-05-05 00:01:00 UTC
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

# Revision identifiers, used by Alembic.
revision: str = "20260505_002"
down_revision: Union[str, None] = "20260505_001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Defensive: lower-case any pre-existing rows that violated the future
    # constraint. In a freshly-seeded prod DB this UPDATE is a no-op (the
    # admin seed already lowercases). Done as a single statement so the
    # constraint addition that follows can be added without NOT VALID.
    op.execute("UPDATE users SET username = lower(username) WHERE username <> lower(username)")
    op.create_check_constraint(
        "ck_users_username_lower",
        "users",
        "username = lower(username)",
    )


def downgrade() -> None:
    op.drop_constraint("ck_users_username_lower", "users", type_="check")
