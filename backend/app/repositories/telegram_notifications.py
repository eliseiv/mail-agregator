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
- :meth:`list_tags_for_recipient` — load the recipient-scoped tags
  applied to the message (used by the notification text formatter).
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
    """A tag applied to a message that belongs to a specific recipient
    (used to render the notification text)."""

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
        """SQL from ADR-0022 §2.2.

        Selects users who: (a) can see the message under visibility rules
        (super_admin / same group / explicit owner), (b) have an active
        ``telegram_links`` row, (c) have at least one of their own
        ``message_tags`` rows on the message, (d) are not opted-out via
        ``users_settings``.

        Returns one row per eligible recipient.
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
            JOIN   message_tags mt ON mt.message_id = m.id
            JOIN   tags t
                   ON t.id = mt.tag_id
                   AND t.user_id = u.id
            LEFT JOIN users_settings us ON us.user_id = u.id
            WHERE  m.id = :message_id
              AND  COALESCE(us.tg_notifications_enabled, true) = true
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

    async def list_tags_for_recipient(self, *, message_id: int, user_id: int) -> list[RecipientTag]:
        """Tags applied to ``message_id`` that belong to ``user_id``.

        Used to render the notification text. Sorted by tag name so the
        output is deterministic.
        """
        stmt = text(
            """
            SELECT t.id, t.name, t.color
            FROM   message_tags mt
            JOIN   tags t ON t.id = mt.tag_id
            WHERE  mt.message_id = :message_id
              AND  t.user_id = :user_id
            ORDER  BY t.name
            """
        )
        result = await self._s.execute(stmt, {"message_id": message_id, "user_id": user_id})
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
