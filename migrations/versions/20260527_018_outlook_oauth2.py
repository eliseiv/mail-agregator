"""OAuth2 (XOAUTH2) support for personal Outlook accounts (ADR-0025, Sprint B).

Extends ``mail_accounts`` so an account can authenticate with OAuth tokens
(``auth_type='oauth_outlook'``) alongside the existing password path
(``auth_type='password'``). No separate ``oauth_tokens`` table — the 1:1
relationship lives in columns on ``mail_accounts`` (ADR-0025 §1).

``up``:
  1. ``auth_type TEXT NOT NULL DEFAULT 'password'``.
  2. ``oauth_provider``, ``oauth_refresh_token_encrypted``,
     ``oauth_access_token_encrypted``, ``oauth_access_token_expires_at``,
     ``oauth_needs_consent NOT NULL DEFAULT false``, ``oauth_scopes``,
     ``proxy_url`` (reserved, TD-029 — not used yet).
  3. ``encrypted_password`` -> drop NOT NULL (oauth accounts have no password).
  4. CHECK constraints (ADR-0025 §7 / docs/03-data-model.md ``mail_accounts``):
     - ``ck_mail_accounts_auth_type``  — auth_type in ('password','oauth_outlook').
     - ``ck_mail_accounts_password_creds`` — password accounts must keep a
       non-NULL ``encrypted_password``.
     - ``ck_mail_accounts_oauth_creds``    — oauth accounts must keep a
       non-NULL ``oauth_refresh_token_encrypted`` and ``oauth_provider='outlook'``.

``down`` is **lossy** by design: restoring ``encrypted_password NOT NULL``
fails while any ``auth_type='oauth_outlook'`` row exists (those rows have a
NULL password). Operators must delete oauth accounts before downgrading; in
prod we never downgrade (forward-only migration policy, deploy/README).

Revision ID: 20260527_018
Revises: 20260527_017
Create Date: 2026-05-27
"""

from __future__ import annotations

from alembic import op

revision = "20260527_018"
down_revision = "20260527_017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---- 1. auth_type ---------------------------------------------------
    op.execute(
        "ALTER TABLE mail_accounts "
        "ADD COLUMN IF NOT EXISTS auth_type TEXT NOT NULL DEFAULT 'password'"
    )

    # ---- 2. oauth_* columns + reserved proxy_url ------------------------
    op.execute("ALTER TABLE mail_accounts ADD COLUMN IF NOT EXISTS oauth_provider TEXT")
    op.execute(
        "ALTER TABLE mail_accounts " "ADD COLUMN IF NOT EXISTS oauth_refresh_token_encrypted BYTEA"
    )
    op.execute(
        "ALTER TABLE mail_accounts " "ADD COLUMN IF NOT EXISTS oauth_access_token_encrypted BYTEA"
    )
    op.execute(
        "ALTER TABLE mail_accounts "
        "ADD COLUMN IF NOT EXISTS oauth_access_token_expires_at TIMESTAMPTZ"
    )
    op.execute(
        "ALTER TABLE mail_accounts "
        "ADD COLUMN IF NOT EXISTS oauth_needs_consent BOOLEAN NOT NULL DEFAULT false"
    )
    op.execute("ALTER TABLE mail_accounts ADD COLUMN IF NOT EXISTS oauth_scopes TEXT")
    # Reserved per-account proxy — NOT used in this sprint (TD-029).
    op.execute("ALTER TABLE mail_accounts ADD COLUMN IF NOT EXISTS proxy_url TEXT")

    # ---- 3. encrypted_password: drop NOT NULL ---------------------------
    op.execute("ALTER TABLE mail_accounts ALTER COLUMN encrypted_password DROP NOT NULL")

    # ---- 4. CHECK constraints (ADR-0025 §7) -----------------------------
    # ``IF NOT EXISTS`` is not available for ADD CONSTRAINT in PG16; drop
    # first (idempotent) then add so re-runs against a partially-migrated DB
    # don't error.
    op.execute("ALTER TABLE mail_accounts DROP CONSTRAINT IF EXISTS ck_mail_accounts_auth_type")
    op.execute(
        "ALTER TABLE mail_accounts "
        "ADD CONSTRAINT ck_mail_accounts_auth_type "
        "CHECK (auth_type IN ('password', 'oauth_outlook'))"
    )

    op.execute(
        "ALTER TABLE mail_accounts DROP CONSTRAINT IF EXISTS ck_mail_accounts_password_creds"
    )
    op.execute(
        "ALTER TABLE mail_accounts "
        "ADD CONSTRAINT ck_mail_accounts_password_creds "
        "CHECK (auth_type <> 'password' OR encrypted_password IS NOT NULL)"
    )

    op.execute("ALTER TABLE mail_accounts DROP CONSTRAINT IF EXISTS ck_mail_accounts_oauth_creds")
    op.execute(
        "ALTER TABLE mail_accounts "
        "ADD CONSTRAINT ck_mail_accounts_oauth_creds "
        "CHECK ("
        "  auth_type <> 'oauth_outlook' "
        "  OR (oauth_refresh_token_encrypted IS NOT NULL AND oauth_provider = 'outlook')"
        ")"
    )


def downgrade() -> None:
    # LOSSY (ADR-0025 §7): re-adding ``encrypted_password NOT NULL`` requires
    # that no oauth_outlook rows (which have a NULL password) exist. We do NOT
    # silently delete them — the ALTER below will error if any remain, which
    # is the correct safety behaviour (operator must remove oauth accounts
    # first). Forward-only policy means this path is dev/test only.
    op.execute("ALTER TABLE mail_accounts DROP CONSTRAINT IF EXISTS ck_mail_accounts_oauth_creds")
    op.execute(
        "ALTER TABLE mail_accounts DROP CONSTRAINT IF EXISTS ck_mail_accounts_password_creds"
    )
    op.execute("ALTER TABLE mail_accounts DROP CONSTRAINT IF EXISTS ck_mail_accounts_auth_type")

    op.execute("ALTER TABLE mail_accounts DROP COLUMN IF EXISTS proxy_url")
    op.execute("ALTER TABLE mail_accounts DROP COLUMN IF EXISTS oauth_scopes")
    op.execute("ALTER TABLE mail_accounts DROP COLUMN IF EXISTS oauth_needs_consent")
    op.execute("ALTER TABLE mail_accounts DROP COLUMN IF EXISTS oauth_access_token_expires_at")
    op.execute("ALTER TABLE mail_accounts DROP COLUMN IF EXISTS oauth_access_token_encrypted")
    op.execute("ALTER TABLE mail_accounts DROP COLUMN IF EXISTS oauth_refresh_token_encrypted")
    op.execute("ALTER TABLE mail_accounts DROP COLUMN IF EXISTS oauth_provider")

    # Restore NOT NULL on encrypted_password (errors if oauth rows remain —
    # intentional).
    op.execute("ALTER TABLE mail_accounts ALTER COLUMN encrypted_password SET NOT NULL")

    op.execute("ALTER TABLE mail_accounts DROP COLUMN IF EXISTS auth_type")
