"""Add ``messages.body_html`` — sanitised HTML body for rich rendering.

Round-12 bug B: the previous data model stored only ``body_text`` (the
``text/plain`` part of the email, or ``html2text``-converted from HTML).
Mailchimp-style senders ship markdown-formatted plain text plus reams of
zero-width invisible padding, which rendered as raw ``[text](url)``
markdown plus rows of invisible characters in the inbox / Telegram.

Storing the original HTML lets the renderer show clickable links and
inline images. The column is nullable: legacy rows have no HTML, and
plain-text-only emails (no ``text/html`` part) leave the column NULL.

Sanitisation happens at ingest time (``worker/app/imap_fetcher.py``)
with ``bleach`` against a strict whitelist — no ``<script>``, no event
handlers, no ``javascript:`` URLs.

Revision ID: 20260513_011
Revises: 20260510_010
Create Date: 2026-05-13
"""

from __future__ import annotations

from alembic import op

revision = "20260513_011"
down_revision = "20260510_010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS body_html TEXT NULL")


def downgrade() -> None:
    op.execute("ALTER TABLE messages DROP COLUMN IF EXISTS body_html")
