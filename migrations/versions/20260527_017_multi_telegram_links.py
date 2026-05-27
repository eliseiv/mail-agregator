"""Multiple Telegram links per internal user (ADR-0024, Sprint A).

Lifts the ADR-0022 invariant "one internal user — at most one Telegram"
and reworks the delivery idempotency key:

1. ``telegram_links``: drop ``UNIQUE(user_id)`` (auto-named
   ``telegram_links_user_id_key`` from the column-level ``UNIQUE`` in
   migration ``20260510_010``). The non-unique
   ``telegram_links_user_id_idx`` already exists and is kept — recipient
   SQL / logout / "my links" all rely on it. ``user_id`` becomes a 1:N
   FK; the PK on ``telegram_user_id`` (TG→user 1:1) is unchanged.

2. ``telegram_notifications``: idempotency key changes from
   ``(message_id, user_id)`` to ``(message_id, telegram_user_id)`` so each
   chat of a multi-linked user gets its own notification row. A new
   ``telegram_user_id BIGINT NOT NULL`` column is added; it is the chat
   the notification was delivered to (no FK — the registry must survive
   link delete/rebind, like ``telegram_message_id``).

   Backfill (ADR-0024 §8): at migration time the 1:1 invariant still holds
   (the soft-limit only relaxes it going forward), so each existing row's
   ``telegram_user_id`` is unambiguous via the user's single link. Rows
   whose link was already deleted get the synthetic value ``0``
   (TD-028 — already delivered, self-cleans via retention cascade).

``down`` is **lossy**: restoring ``UNIQUE(message_id, user_id)`` requires
de-duplicating rows that became multi-chat (we keep ``MIN(id)`` per
``(message_id, user_id)``), and dropping the ``telegram_user_id`` column
discards the per-chat detail. Documented as a one-way data loss.

Revision ID: 20260527_017
Revises: 20260521_016
Create Date: 2026-05-27
"""

from __future__ import annotations

from alembic import op

revision = "20260527_017"
down_revision = "20260521_016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---- 1. telegram_links: drop UNIQUE(user_id) ------------------------
    # Migration 010 declared ``user_id BIGINT NOT NULL UNIQUE`` inline, so
    # PostgreSQL named the constraint ``telegram_links_user_id_key``. Drop it
    # idempotently (``IF EXISTS``) — the non-unique
    # ``telegram_links_user_id_idx`` (also from migration 010) stays and now
    # carries the full read load.
    op.execute("ALTER TABLE telegram_links DROP CONSTRAINT IF EXISTS telegram_links_user_id_key")
    # Defence-in-depth: ensure the non-unique helper index still exists (it
    # was created in migration 010; ``IF NOT EXISTS`` is a no-op there).
    op.execute("CREATE INDEX IF NOT EXISTS telegram_links_user_id_idx ON telegram_links(user_id)")

    # ---- 2. telegram_notifications: add telegram_user_id (nullable) -----
    op.execute(
        "ALTER TABLE telegram_notifications ADD COLUMN IF NOT EXISTS telegram_user_id BIGINT"
    )

    # ---- 3. backfill from the (still 1:1) telegram_links ----------------
    # At this point each user has at most one link, so the subquery returns a
    # single deterministic chat. LIMIT 1 is belt-and-suspenders.
    op.execute(
        """
        UPDATE telegram_notifications tn
        SET    telegram_user_id = (
                   SELECT tl.telegram_user_id
                   FROM   telegram_links tl
                   WHERE  tl.user_id = tn.user_id
                   LIMIT  1
               )
        WHERE  tn.telegram_user_id IS NULL
        """
    )

    # ---- 4. orphaned rows (link already deleted) → synthetic 0 (TD-028) -
    op.execute(
        "UPDATE telegram_notifications SET telegram_user_id = 0 WHERE telegram_user_id IS NULL"
    )

    # ---- 5. enforce NOT NULL --------------------------------------------
    op.execute("ALTER TABLE telegram_notifications ALTER COLUMN telegram_user_id SET NOT NULL")

    # ---- 6. swap the idempotency key ------------------------------------
    op.execute(
        "ALTER TABLE telegram_notifications DROP CONSTRAINT IF EXISTS telegram_notifications_unique"
    )
    op.execute(
        """
        ALTER TABLE telegram_notifications
        ADD CONSTRAINT telegram_notifications_msg_chat_uq
            UNIQUE (message_id, telegram_user_id)
        """
    )


def downgrade() -> None:
    # LOSSY (ADR-0024 §8): rows that became multi-chat under the new key
    # collide on the old ``(message_id, user_id)`` key. De-duplicate keeping
    # the earliest row per pair, then restore the old constraint and drop the
    # per-chat column. The discarded rows / chat detail cannot be recovered.
    op.execute(
        "ALTER TABLE telegram_notifications DROP CONSTRAINT IF EXISTS telegram_notifications_msg_chat_uq"
    )
    op.execute(
        """
        DELETE FROM telegram_notifications a
        USING  telegram_notifications b
        WHERE  a.message_id = b.message_id
          AND  a.user_id    = b.user_id
          AND  a.id > b.id
        """
    )
    op.execute(
        """
        ALTER TABLE telegram_notifications
        ADD CONSTRAINT telegram_notifications_unique UNIQUE (message_id, user_id)
        """
    )
    op.execute("ALTER TABLE telegram_notifications DROP COLUMN IF EXISTS telegram_user_id")

    # Restore UNIQUE(user_id) on telegram_links. This too is lossy if a user
    # acquired multiple links while on the new schema — keep the earliest
    # link per user before re-adding the constraint.
    #
    # The dedup orders by ``(created_at, telegram_user_id)`` as a composite
    # key: ``created_at`` picks the earliest link, and ``telegram_user_id``
    # (the table PK) is a deterministic tiebreak so a batch-insert that gives
    # two links of the same user an identical ``created_at`` still collapses
    # to exactly one survivor. Without the PK tiebreak both rows fail the
    # ``a.created_at > b.created_at`` predicate, survive, and the
    # ``UNIQUE(user_id)`` re-add below then errors on the duplicate.
    op.execute(
        """
        DELETE FROM telegram_links a
        USING  telegram_links b
        WHERE  a.user_id = b.user_id
          AND  a.telegram_user_id <> b.telegram_user_id
          AND  (a.created_at, a.telegram_user_id) > (b.created_at, b.telegram_user_id)
        """
    )
    op.execute(
        "ALTER TABLE telegram_links ADD CONSTRAINT telegram_links_user_id_key UNIQUE (user_id)"
    )
