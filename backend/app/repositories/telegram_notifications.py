"""Repository for ``telegram_notifications`` (ADR-0022 §2.3 + §2.8).

Implements:

- :meth:`try_reserve` — claim ``(message_id, telegram_user_id)`` exclusively
  before dispatch (ADR-0024 §6 — per-chat dedup). ON CONFLICT DO NOTHING
  means a returning empty result is the signal "already delivered / claimed
  for this chat".
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

from shared.config import get_settings
from shared.models import TelegramNotification

# ADR-0022 §2.2 / §2.8: tag predicate fragment, appended to the recipient
# SQL only when TG_NOTIFY_ALL_MESSAGES is off (historical "tagged-only"
# behaviour). When the flag is on (default) the fragment is the empty string
# — a message without any tag is still a valid notification target.
_TAG_PREDICATE_SQL = """
              AND  EXISTS (
                       SELECT 1
                       FROM   message_tags mt
                       WHERE  mt.message_id = m.id
                   )"""


def _tag_predicate() -> str:
    """Return the conditional ``<TAG_PREDICATE>`` fragment (ADR-0022 §2.2).

    Structural SQL substitution (not a bind parameter): empty string when
    ``TG_NOTIFY_ALL_MESSAGES`` is on, the ``EXISTS(message_tags)`` block when
    off. Read from the lru-cached settings so a flag flip needs only a worker
    restart, not a redeploy.
    """
    return "" if get_settings().TG_NOTIFY_ALL_MESSAGES else _TAG_PREDICATE_SQL


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

    async def try_reserve(
        self, *, message_id: int, user_id: int, telegram_user_id: int
    ) -> int | None:
        """Claim ``(message_id, telegram_user_id)`` for delivery to one chat.

        ADR-0024 §6: the idempotency key is the **chat**, not the user — a
        user with several links gets one row per chat. ``user_id`` is still
        stored (audit / recovery JOIN). ON CONFLICT DO NOTHING means an empty
        RETURNING signals "already delivered / claimed for this chat".
        Returns the row id, or ``None`` if a row already existed.
        """
        stmt = (
            pg_insert(TelegramNotification)
            .values(
                message_id=message_id,
                user_id=user_id,
                telegram_user_id=telegram_user_id,
            )
            .on_conflict_do_nothing(
                index_elements=[
                    TelegramNotification.message_id,
                    TelegramNotification.telegram_user_id,
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
        (c) the message satisfies the conditional tag predicate
            (round-31): when ``TG_NOTIFY_ALL_MESSAGES`` is on (default) the
            predicate is **absent** — every visible message is eligible,
            tagged or not; when off, the message must have **at least one**
            tag applied (any tag, any user) — auto-tagging tags only the
            mailbox owner, but every group-mate should also be notified;
        (d) are not opted-out via ``users_settings``;
        (e) the message arrived *at or after* the moment the user linked
            their Telegram account (``m.internal_date >= tl.created_at``).

        Returns one row per eligible recipient.

        Round-12 (bug A): the previous SQL required the recipient to own
        a tag on the message via ``JOIN tags t ON t.user_id = u.id`` —
        which excluded every group member whose leader tagged the
        message. We then required only "the message has any tag at all"
        via ``EXISTS (... message_tags ...)``; the per-user filter is
        gone.

        Round-31 (notify about ALL messages): that ``EXISTS(message_tags)``
        block is now **conditional** on ``TG_NOTIFY_ALL_MESSAGES`` (see
        :func:`_tag_predicate`). The visibility / active-link / first-link
        (``m.internal_date >= tl.created_at``) / opt-out invariants are
        unchanged in both modes — only the tag predicate toggles.

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
            f"""
            SELECT DISTINCT
                   u.id              AS user_id,
                   tl.telegram_user_id AS telegram_user_id,
                   ma.id              AS mail_account_id
            FROM   messages m
            JOIN   mail_accounts ma ON ma.id = m.mail_account_id
            JOIN   users u
                   ON (
                       u.role = 'super_admin'
                       OR (ma.group_id IS NOT NULL AND EXISTS (
                              SELECT 1 FROM user_groups ug
                              WHERE  ug.user_id = u.id
                                AND  ug.group_id = ma.group_id
                          ))
                       OR u.id = ma.user_id
                   )
            JOIN   telegram_links tl
                   ON tl.user_id = u.id
                   AND tl.dead_at IS NULL
                   AND m.internal_date >= tl.created_at
            LEFT JOIN users_settings us ON us.user_id = u.id
            WHERE  m.id = :message_id
              AND  COALESCE(us.tg_notifications_enabled, true) = true{_tag_predicate()}
            """
            # The f-string only interpolates ``_tag_predicate()`` — a fixed
            # internal SQL constant, never user input. No injection surface.
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

    async def list_recipients_for_mailbox(self, *, mail_account_id: int) -> list[NotifyRecipient]:
        """Recipients of a mailbox-down alert (ADR-0033 §3).

        Twin of :meth:`list_recipients_for_message` but resolved **by mailbox**
        instead of by message. Same visibility predicate (super_admin OR
        membership in the mailbox's team via ``user_groups`` per ADR-0030 OR
        the owner) + a live ``telegram_links`` row (``dead_at IS NULL``) +
        opt-out via ``users_settings.tg_notifications_enabled``, but **without**
        any per-message predicate:

        - **No** ``m.internal_date >= tl.created_at`` (first-link backfill guard)
          — a mailbox-down alert has no message/time; disabling is a *current*
          operational event that any *currently* linked recipient must receive,
          regardless of when they linked Telegram.
        - **No** tag predicate / ``TG_NOTIFY_ALL_MESSAGES`` toggle — tags relate
          to messages, not to mailbox state.

        Returns one row per eligible recipient. ``mail_account_id`` is carried
        into each :class:`NotifyRecipient` from the input (there is no message
        here) so the caller keeps the shared dataclass. Per-chat dedup by
        ``telegram_user_id`` happens in the dispatcher (§14.3).
        """
        stmt = text(
            """
            SELECT DISTINCT
                   u.id                AS user_id,
                   tl.telegram_user_id AS telegram_user_id
            FROM   mail_accounts ma
            JOIN   users u
                   ON (
                       u.role = 'super_admin'
                       OR (ma.group_id IS NOT NULL AND EXISTS (
                              SELECT 1 FROM user_groups ug
                              WHERE  ug.user_id = u.id
                                AND  ug.group_id = ma.group_id
                          ))
                       OR u.id = ma.user_id
                   )
            JOIN   telegram_links tl
                   ON tl.user_id = u.id
                   AND tl.dead_at IS NULL
            LEFT JOIN users_settings us ON us.user_id = u.id
            WHERE  ma.id = :mail_account_id
              AND  COALESCE(us.tg_notifications_enabled, true) = true
            """
        )
        result = await self._s.execute(stmt, {"mail_account_id": mail_account_id})
        return [
            NotifyRecipient(
                user_id=int(row.user_id),
                telegram_user_id=int(row.telegram_user_id),
                mail_account_id=mail_account_id,
            )
            for row in result
        ]

    async def list_missing_for_recovery(self, *, window_hours: int, limit: int) -> list[int]:
        """SQL from ADR-0022 §2.8 (round-33 per-recipient; round-35 / ADR-0024
        §7 per-chat).

        Returns ``DISTINCT message_id`` values that still have a **visible,
        linked chat without a** ``telegram_notifications`` row by
        ``(message_id, telegram_user_id)`` within the lookback window.

        ADR-0024 §7 (per-chat): the ``UNIQUE(user_id)`` on ``telegram_links``
        is gone, so the ``JOIN telegram_links tl`` yields one row per **live
        chat** of each visible user. The ``NOT EXISTS`` therefore compares
        ``tn.telegram_user_id = tl.telegram_user_id`` (not ``tn.user_id``):
        recovery picks the message up while *any* visible chat lacks a row.

        Round-33 (CRITICAL fix this builds on): a per-message
        ``NOT EXISTS (tn WHERE tn.message_id = m.id)`` masked partial delivery
        — if chat A was delivered but chat B throttled (§2.9: ``continue``
        without ``try_reserve``, no row), the per-message check returned FALSE
        and B lost the notification forever. Per-``(message_id, user_id)``
        (round-33) then would have lost a *second* chat of the same user once
        multi-TG landed; per-chat (round-35) fixes that.

        The query reuses the **same recipient logic as §2.2** (visibility
        super_admin/group/owner; active ``telegram_links`` with
        ``m.internal_date >= tl.created_at``; opt-out via ``users_settings``;
        conditional tag predicate under ``TG_NOTIFY_ALL_MESSAGES``).

        ``DISTINCT m.id`` collapses multiple undelivered chats of the same
        message to a single re-enqueue (dispatch resolves all recipients
        itself; already-delivered chats are skipped by ``try_reserve``).

        The 24h window is a deliberate cap: older messages are skipped to
        avoid spamming users about stale mail (TD-027 documents the sustained
        throttle edge case).
        """
        cutoff = datetime.now(UTC) - timedelta(hours=window_hours)
        stmt = text(
            f"""
            SELECT DISTINCT m.id
            FROM   messages m
            JOIN   mail_accounts ma ON ma.id = m.mail_account_id
            JOIN   users u
                   ON (
                       u.role = 'super_admin'
                       OR (ma.group_id IS NOT NULL AND EXISTS (
                              SELECT 1 FROM user_groups ug
                              WHERE  ug.user_id = u.id
                                AND  ug.group_id = ma.group_id
                          ))
                       OR u.id = ma.user_id
                   )
            JOIN   telegram_links tl
                   ON tl.user_id = u.id
                   AND tl.dead_at IS NULL
                   AND m.internal_date >= tl.created_at
            LEFT JOIN users_settings us ON us.user_id = u.id
            WHERE  m.fetched_at > :cutoff
              AND  COALESCE(us.tg_notifications_enabled, true) = true{_tag_predicate()}
              AND  NOT EXISTS (
                       SELECT 1 FROM telegram_notifications tn
                       WHERE  tn.message_id      = m.id
                         AND  tn.telegram_user_id = tl.telegram_user_id
                   )
            ORDER  BY m.id
            LIMIT  :limit
            """
            # The f-string only interpolates ``_tag_predicate()`` — a fixed
            # internal SQL constant, never user input. No injection surface.
        )
        result = await self._s.execute(stmt, {"cutoff": cutoff, "limit": int(limit)})
        return [int(row.id) for row in result]
