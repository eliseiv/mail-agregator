"""Rebuild the builtin-tag catalogue for the App Store Connect workflow.

round-25 reworked the builtin-tag catalogue (``backend/app/tags/builtin.py``):
the old set ("DPLA.PLA", "Диспут", "Отменить подписку", "Продление
аккаунта") was replaced with a larger App Store Connect oriented set, the
"Диспут" rule lost its ``Apple Inc`` subject clause, "Отменить подписку"
switched to ``match_mode='all'``, and several tags now rely on
``sender_contains`` matching the sender *display-name* (round-25 SQL change).

``TagsService.ensure_builtin_tags`` is idempotent: it short-circuits as soon
as a user owns *any* ``is_builtin`` row (``has_any_builtin``). That means
existing users — who already own the *old* builtin set — would never receive
the new catalogue. We need to force a rebuild.

Strategy chosen: **(a) DELETE-only, recreate on next login.**

We simply ``DELETE FROM tags WHERE is_builtin = true``. The next time each
user logs in, ``AuthService.login`` / the SSO path call
``ensure_builtin_tags`` (a post-login hook fired on *every* login, not just
the first — see ``backend/app/auth/service.py``), see ``has_any_builtin``
is now false, and recreate the fresh catalogue with the correct
``match_mode`` for that user.

Option (b) — re-INSERTing the full catalogue for every existing ``user_id``
directly in SQL — was rejected: it would duplicate the catalogue definition
(name/colour/rules/match_mode) inside the migration, where it would rot out
of sync with ``builtin.py``, and it buys little because login is frequent.
The only cost of (a) is that a user's new builtin tags appear after their
next login rather than instantly; that is acceptable.

DATA LOSS: this DELETE removes the old builtin ``tags`` rows. The FK
``ON DELETE CASCADE`` from ``tag_rules`` and ``message_tags`` to ``tags``
(see 20260507_003_add_tags) means the old builtin tags' rules **and their
applied message_tags links are dropped too**. This is intentional — the new
rules differ, so old auto-tagging is stale. New mail is auto-tagged on
arrival; for historical mail the user clicks "Применить к существующим"
(apply-to-existing) on the relevant tag. Custom (non-builtin) tags and their
links are untouched.

This migration is NOT cleanly reversible — ``downgrade`` cannot resurrect the
deleted rows. It is left as a documented no-op rather than recreating an
arbitrary historical catalogue.

Revision ID: 20260521_016
Revises: 20260521_015
Create Date: 2026-05-21
"""

from __future__ import annotations

from alembic import op

revision = "20260521_016"
down_revision = "20260521_015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # DATA LOSS (documented above): drops every builtin tag; CASCADE removes
    # the associated tag_rules and message_tags. ensure_builtin_tags recreates
    # the new catalogue on each user's next login.
    op.execute("DELETE FROM tags WHERE is_builtin = true")


def downgrade() -> None:
    # Irreversible: the deleted builtin rows cannot be reconstructed here.
    # Recreation happens organically via ensure_builtin_tags on next login,
    # so a downgrade is a deliberate no-op.
    pass
