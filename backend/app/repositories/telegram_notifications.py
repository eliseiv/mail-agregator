"""Repository for ``telegram_notifications`` (ADR-0022 §2.3 + §2.8).

Implements:

- :meth:`try_reserve` — claim ``(message_id, user_id)`` exclusively before
  dispatch. ON CONFLICT DO NOTHING means a returning empty result is the
  signal "already delivered / claimed elsewhere".
- :meth:`mark_sent` — finalise the row with ``telegram_message_id``.
- :meth:`rollback` — delete the row if dispatch failed at the network
  layer so the retry path can re-claim.
- :meth:`list_recipients_for_message` — full SQL from ADR-0022 §2.2:
  who should receive a notification for a given ``message_id``.
- :meth:`list_tags_for_message` — load every tag applied to the message
  (used by the notification text formatter). Round-12: changed from
  per-recipient filtering to "all tags on the message" so group members
  receive notifications about messages whose mailbox owner (the leader)
  has tagged them — see the round-12 bug A fix in
  :mod:`backend.app.telegram.notify_service`.
- :meth:`list_missing_for_recovery` — recovery_scan query from §2.8.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models import TelegramNotification


@dataclass(frozen=True, slots=True)
class NotifyRecipient:
    """Resolved recipient of a Telegram notification.

    Fields:

    - ``user_id``         — internal user id (FK into ``users``).
    - ``telegram_user_id``— chat_id to POST sendMessage to.
    - ``mail_account_id`` — the account that received the message
      (for the notification text).
    """

    user_id: int
    telegram_user_id: int
    mail_account_id: int


@dataclass(frozen=True, slots=True)
class RecipientTag:
    """A tag applied to a message (used to render the notification text).

    Round-12 bug A: previously this was a "per-recipient" tag (only the
    recipient's own tags were returned). Now it represents *any* tag on
    the message — every recipient sees the same set, which matches the
    visibility model: if a user can see the mailbox, they can see all
    auto-applied tags on its messages.
    """

    id: int
    name: str
    color: str


class TelegramNotificationsRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    # --- Writes ------------------------------------------------------------

    async def try_reserve(self, *, message_id: int, user_id: int) -> int | None:
        """Claim ``(message_id, user_id)``. Returns the row id, or None if
        a row already existed (idempotent: don't double-deliver)."""
        stmt = (
            pg_insert(TelegramNotification)
            .values(message_id=message_id, user_id=user_id)
            .on_conflict_do_nothing(
                index_elements=[
                    TelegramNotification.message_id,
                    TelegramNotification.user_id,
                ]
            )
            .returning(TelegramNotification.id)
        )
        result = await self._s.execute(stmt)
        row = result.first()
        if row is None:
            return None
        return int(row[0])

    async def mark_sent(self, *, notification_id: int, telegram_message_id: int | None) -> None:
        """Finalise a claimed row after a successful sendMessage."""
        await self._s.execute(
            update(TelegramNotification)
            .where(TelegramNotification.id == notification_id)
            .values(
                sent_at=datetime.now(UTC),
                telegram_message_id=telegram_message_id,
            )
        )

    async def rollback(self, *, notification_id: int) -> None:
        """Delete a previously-claimed row so the dispatcher can retry.

        Used only for transient errors (network / 5xx). For 403/400 we
        keep the row (with ``sent_at IS NULL``) as an audit marker that
        we attempted and were refused.
        """
        await self._s.execute(
            delete(TelegramNotification).where(TelegramNotification.id == notification_id)
        )

    # --- Reads -------------------------------------------------------------

    async def list_recipients_for_message(self, *, message_id: int) -> list[NotifyRecipient]:
        """SQL from ADR-0022 §2.2 (round-12 bug A fix + round-13 first-link
        backfill fix).

        Selects users who:

        (a) can see the message under visibility rules (super_admin /
            same group / explicit owner — same as ADR-0019 §7);
        (b) have an active ``telegram_links`` row;
        (c) the message has **at least one** tag applied (any tag, any
            user) — auto-tagging tags only the mailbox owner, but every
            group-mate should also be notified;
        (d) are not opted-out via ``users_settings``;
        (e) the message arrived *at or after* the moment the user linked
            their Telegram account (``m.internal_date >= tl.created_at``).

        Returns one row per eligible recipient.

        Round-12 (bug A): the previous SQL required the recipient to own
        a tag on the message via ``JOIN tags t ON t.user_id = u.id`` —
        which excluded every group member whose leader tagged the
        message. We now require only "the message has any tag at all"
        via ``EXISTS (... message_tags ...)``; the per-user filter is
        gone.

        Round-13 (first-link backfill bug): on first link the user has
        no ``telegram_notifications`` rows, so the recovery scan picked
        up every historic tagged message visible to them and flooded
        the chat. The ``m.internal_date >= tl.created_at`` predicate
        scopes notifications to mail that arrived *after* linking, both
        for the recovery path and (trivially) for the sync path (links
        are always created before any new mail can arrive for them).
        ``internal_date`` (origin time at the IMAP source) is used
        rather than ``fetched_at`` so a delayed backfill of historic
        mail also stays silent for first-time linkers.
        """
        stmt = text(
            """
            SELECT DISTINCT
                   u.id              AS user_id,
                   tl.telegram_user_id AS telegram_user_id,
                   ma.id              AS mail_account_id
            FROM   messages m
            JOIN   mail_accounts ma ON ma.id = m.mail_account_id
            JOIN   users u
                   ON (
                       u.role = 'super_admin'
                       OR (ma.group_id IS NOT NULL AND u.group_id = ma.group_id)
                       OR u.id = ma.user_id
                   )
            JOIN   telegram_links tl
                   ON tl.user_id = u.id
                   AND tl.dead_at IS NULL
                   AND m.internal_date >= tl.created_at
            LEFT JOIN users_settings us ON us.user_id = u.id
            WHERE  m.id = :message_id
              AND  COALESCE(us.tg_notifications_enabled, true) = true
              AND  EXISTS (
                       SELECT 1
                       FROM   message_tags mt
                       WHERE  mt.message_id = m.id
                   )
            """
        )
        result = await self._s.execute(stmt, {"message_id": message_id})
        return [
            NotifyRecipient(
                user_id=int(row.user_id),
                telegram_user_id=int(row.telegram_user_id),
                mail_account_id=int(row.mail_account_id),
            )
            for row in result
        ]

    async def list_tags_for_message(self, *, message_id: int) -> list[RecipientTag]:
        """Every tag applied to ``message_id`` (deduplicated by tag id).

        Round-12 bug A: replaces ``list_tags_for_recipient`` which used to
        scope tags to a single user. The new model is "all recipients see
        the same tag set" — the mailbox owner is the natural source of
        truth for tagging, and there is no UX value in hiding tag names
        from a group-mate who can already open the message.

        Sorted by tag id for stable display order (the per-message
        ordering corresponds to the order auto-tagging applied them).
        """
        stmt = text(
            """
            SELECT t.id, t.name, t.color
            FROM   message_tags mt
            JOIN   tags t ON t.id = mt.tag_id
            WHERE  mt.message_id = :message_id
            ORDER  BY mt.tag_id
            """
        )
        result = await self._s.execute(stmt, {"message_id": message_id})
        return [
            RecipientTag(id=int(row.id), name=str(row.name), color=str(row.color)) for row in result
        ]

    async def list_missing_for_recovery(self, *, window_hours: int, limit: int) -> list[int]:
        """SQL from ADR-0022 §2.8.

        Returns ``message_id`` values that have tags but no
        ``telegram_notifications`` row at all yet (no recipient was claimed
        — usually because the worker crashed between LPUSH and LPOP).

        The 24h window is a deliberate cap: older messages are skipped to
        avoid spamming users about stale mail.
        """
        cutoff = datetime.now(UTC) - timedelta(hours=window_hours)
        stmt = text(
            """
            SELECT m.id
            FROM   messages m
            WHERE  m.fetched_at > :cutoff
              AND  EXISTS (
                       SELECT 1 FROM message_tags mt
                       WHERE  mt.message_id = m.id
                   )
              AND  NOT EXISTS (
                       SELECT 1 FROM telegram_notifications tn
                       WHERE  tn.message_id = m.id
                   )
            ORDER  BY m.id
            LIMIT  :limit
            """
        )
        result = await self._s.execute(stmt, {"cutoff": cutoff, "limit": int(limit)})
        return [int(row.id) for row in result]
