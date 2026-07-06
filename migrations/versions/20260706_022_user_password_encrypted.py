"""Reversible login-password copy for admin display (ADR-0038).

Adds ``users.password_encrypted BYTEA NULL`` — an AES-256-GCM reversible
copy of the login password kept ONLY so a super_admin can reveal it in the
``/admin`` "Password" column. The argon2 ``password_hash`` remains the source
of truth for login verification (ADR-0006); this column never participates in
authentication.

Forward-only per ``07-deployment.md`` migration policy: ``ADD COLUMN``
nullable, no backfill. Existing rows stay ``NULL`` → the UI column shows "—"
until the password is set again by the admin (create/reset) or the user
(self-set). ``admin_audit.action`` gains two new values
(``user_password_set`` / ``user_password_revealed``) but ``action`` is
free-form TEXT at the DB level, so no DDL is needed — the closed set is
enforced in :mod:`backend.app.audit.service`.

Revision ID: 20260706_022
Revises: 20260703_021
Create Date: 2026-07-06
"""

from __future__ import annotations

from alembic import op

revision = "20260706_022"
down_revision = "20260703_021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS password_encrypted BYTEA NULL")


def downgrade() -> None:
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS password_encrypted")
