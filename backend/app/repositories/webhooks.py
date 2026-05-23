"""Repositories for outbound webhooks (ADR-0023 §3).

Two repos, one per table:

- :class:`WebhooksRepo`           — CRUD and dispatcher-side mutations on
  the per-group configuration row (``webhooks``).
- :class:`WebhookDeliveriesRepo`  — claim/finalise/rollback the per-event
  idempotency rows (``webhook_deliveries``), plus the SQL queries used
  by the dispatcher (recipient resolution, tag aggregation, recovery
  scan).

Neither repo opens its own transactions — the caller (service or worker
job) wraps the work in ``async with db.begin():`` so audit and webhook
writes commit atomically.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models import Webhook, WebhookDelivery

# --- DTOs returned by the dispatcher-side SELECTs ---------------------------


@dataclass(frozen=True, slots=True)
class WebhookRecipient:
    """One row of the dispatcher's recipient SQL (ADR-0023 §3.2).

    Fields:

    - ``webhook_id``      — PK of the target webhook.
    - ``group_id``        — owning team (for log/audit context).
    - ``url``             — destination POST URL.
    - ``secret_encrypted``— ciphertext blob (AES-256-GCM, AAD=webhook_id).
    """

    webhook_id: int
    group_id: int
    url: str
    secret_encrypted: bytes


@dataclass(frozen=True, slots=True)
class WebhookTeamTag:
    """A tag aggregated across the whole team for the dispatcher payload.

    Order matches the DB ``ORDER BY t.name`` — stable for receivers.
    """

    id: int
    name: str
    color: str


# --- WebhooksRepo ----------------------------------------------------------


class WebhooksRepo:
    """CRUD + state mutations on ``webhooks``."""

    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    # --- Reads ------------------------------------------------------------

    async def get_by_id(self, webhook_id: int) -> Webhook | None:
        return await self._s.get(Webhook, webhook_id)

    async def get_by_group_id(self, group_id: int) -> Webhook | None:
        stmt = text("SELECT * FROM webhooks WHERE group_id = :gid")
        result = await self._s.execute(stmt, {"gid": group_id})
        row = result.mappings().first()
        if row is None:
            return None
        # Re-hydrate via ORM ``get`` so SQLAlchemy state-management is
        # consistent with the rest of the codebase.
        return await self._s.get(Webhook, int(row["id"]))

    async def find_active_for_message(
        self, *, message_id: int, mail_account_id: int
    ) -> WebhookRecipient | None:
        """SQL from ADR-0023 §3.2.

        Returns the (single) active webhook that should receive this
        ``message_id``, or ``None`` if any of the following holds:

        - the mailbox's team has no webhook configured;
        - the webhook is ``is_active=false`` or has a ``dead_at``;
        - the message's ``internal_date`` is older than the webhook's
          ``created_at`` (history-flood filter, symmetric to round-13
          for TG-notifications);
        - the message has no team tag applied (by a group member or the
          mailbox owner). round-28: a super_admin's personal tag does NOT
          count here — the webhook channel is isolated from super_admin
          tags (ADR-0023 §3.2).
        """
        stmt = text(
            """
            SELECT
                w.id              AS webhook_id,
                w.group_id        AS group_id,
                w.url             AS url,
                w.secret_encrypted AS secret_encrypted
            FROM   webhooks w
            JOIN   mail_accounts ma ON ma.group_id = w.group_id
            JOIN   messages m ON m.id = :message_id
            WHERE  ma.id = :mail_account_id
              AND  w.is_active = TRUE
              AND  w.dead_at IS NULL
              AND  m.internal_date >= w.created_at
              AND  EXISTS (
                       SELECT 1
                       FROM   message_tags mt
                       JOIN   tags t ON t.id = mt.tag_id
                       JOIN   users u ON u.id = t.user_id
                       WHERE  mt.message_id = m.id
                         AND  (
                                 u.group_id = ma.group_id
                                 OR u.id = ma.user_id
                              )
                         -- round-28: NO ``u.role = 'super_admin'`` branch.
                         -- super_admin's personal tags are attached to other
                         -- teams' messages for TG-notifications (ADR-0017
                         -- §5.1), but a team webhook must NOT be triggered by
                         -- them — otherwise a message tagged ONLY by a
                         -- super_admin tag would falsely fire another team's
                         -- webhook. See ADR-0023 §3.2 "Изоляция от персональных
                         -- тегов super_admin".
                   )
            LIMIT 1
            """
        )
        result = await self._s.execute(
            stmt, {"message_id": message_id, "mail_account_id": mail_account_id}
        )
        row = result.mappings().first()
        if row is None:
            return None
        return WebhookRecipient(
            webhook_id=int(row["webhook_id"]),
            group_id=int(row["group_id"]),
            url=str(row["url"]),
            secret_encrypted=bytes(row["secret_encrypted"]),
        )

    # --- Writes -----------------------------------------------------------

    async def next_webhook_id(self) -> int:
        """``SELECT nextval('webhooks_id_seq')``.

        Symmetric to :meth:`MailAccountsRepo.next_account_id` — lets the
        service know the row id before encryption so the AAD can bind to
        it (ADR-0023 §4.1).
        """
        row = await self._s.execute(text("SELECT nextval('webhooks_id_seq')"))
        return int(row.scalar_one())

    async def insert_with_id(
        self,
        *,
        webhook_id: int,
        group_id: int,
        url: str,
        secret_encrypted: bytes,
    ) -> Webhook:
        """INSERT a row with a pre-reserved id (so AAD bind works)."""
        webhook = Webhook(
            id=webhook_id,
            group_id=group_id,
            url=url,
            secret_encrypted=secret_encrypted,
            is_active=True,
            consecutive_failures=0,
        )
        self._s.add(webhook)
        await self._s.flush()
        await self._s.refresh(webhook)
        return webhook

    async def update_fields(self, webhook_id: int, **fields: object) -> None:
        """Generic ``UPDATE webhooks SET ... WHERE id=:id``.

        The trigger ``trg_webhooks_updated_at`` keeps ``updated_at`` in
        sync server-side, so callers don't have to pass it.
        """
        if not fields:
            return
        await self._s.execute(update(Webhook).where(Webhook.id == webhook_id).values(**fields))

    async def delete(self, webhook_id: int) -> None:
        """``DELETE FROM webhooks WHERE id=:id`` — CASCADE clears
        ``webhook_deliveries``."""
        await self._s.execute(delete(Webhook).where(Webhook.id == webhook_id))

    async def update_secret(self, webhook_id: int, secret_encrypted: bytes) -> None:
        await self._s.execute(
            update(Webhook)
            .where(Webhook.id == webhook_id)
            .values(secret_encrypted=secret_encrypted)
        )

    async def mark_success(self, webhook_id: int) -> None:
        """2xx delivery — reset failure counter + clear last_error."""
        await self._s.execute(
            update(Webhook)
            .where(Webhook.id == webhook_id)
            .values(
                last_fired_at=datetime.now(UTC),
                consecutive_failures=0,
                last_error=None,
            )
        )

    async def bump_failure(self, webhook_id: int, last_error: str) -> int:
        """Increment ``consecutive_failures`` atomically; return new value.

        Used for non-retriable 4xx responses (ADR-0023 §3.4 step 10). The
        dispatcher caller compares the result against
        ``settings.WEBHOOK_MAX_FAILURES_BEFORE_DEAD`` to decide whether to
        also call :meth:`mark_dead`.
        """
        # ``last_error`` is clamped at 500 chars to fit ADR-0023 §1.1
        # ("truncated to 500 bytes"); the column itself has no length
        # constraint but we keep the row narrow.
        last_error_clamped = last_error[:500] if last_error else None
        stmt = text(
            """
            UPDATE webhooks
            SET    consecutive_failures = consecutive_failures + 1,
                   last_error           = :err
            WHERE  id = :id
            RETURNING consecutive_failures
            """
        )
        result = await self._s.execute(stmt, {"id": webhook_id, "err": last_error_clamped})
        row = result.first()
        if row is None:
            # Row deleted between dispatch and bump — defensive return 0.
            return 0
        return int(row[0])

    async def touch_last_error(self, webhook_id: int, last_error: str) -> None:
        """Record a transient failure WITHOUT incrementing the counter."""
        last_error_clamped = last_error[:500] if last_error else None
        await self._s.execute(
            update(Webhook).where(Webhook.id == webhook_id).values(last_error=last_error_clamped)
        )

    async def mark_dead(self, webhook_id: int, reason: str) -> None:
        """Set ``dead_at = now()`` and record the reason in ``last_error``."""
        reason_clamped = reason[:500] if reason else None
        await self._s.execute(
            update(Webhook)
            .where(Webhook.id == webhook_id)
            .values(
                dead_at=datetime.now(UTC),
                last_error=reason_clamped,
            )
        )

    async def mark_alive(self, webhook_id: int) -> None:
        """Re-enable a dead webhook: ``PATCH is_active=true`` flow.

        Clears ``dead_at``, ``consecutive_failures`` and ``last_error``
        in one statement so the dispatcher's next pass sees a fully
        healthy row.
        """
        await self._s.execute(
            update(Webhook)
            .where(Webhook.id == webhook_id)
            .values(
                dead_at=None,
                consecutive_failures=0,
                last_error=None,
                is_active=True,
            )
        )


# --- WebhookDeliveriesRepo --------------------------------------------------


class WebhookDeliveriesRepo:
    """Claim / finalise / rollback the per-event idempotency rows."""

    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    # --- Writes -----------------------------------------------------------

    async def try_reserve(self, *, webhook_id: int, message_id: int) -> int | None:
        """Claim ``(webhook_id, message_id)``. Returns the row id, or ``None``
        if a row already existed (idempotency: don't double-deliver)."""
        stmt = (
            pg_insert(WebhookDelivery)
            .values(webhook_id=webhook_id, message_id=message_id)
            .on_conflict_do_nothing(
                index_elements=[
                    WebhookDelivery.webhook_id,
                    WebhookDelivery.message_id,
                ]
            )
            .returning(WebhookDelivery.id)
        )
        result = await self._s.execute(stmt)
        row = result.first()
        if row is None:
            return None
        return int(row[0])

    async def mark_sent(
        self, *, delivery_id: int, response_code: int, response_excerpt: str
    ) -> None:
        """Finalise a claim after a successful (2xx) POST."""
        excerpt = (response_excerpt or "")[:500]
        await self._s.execute(
            update(WebhookDelivery)
            .where(WebhookDelivery.id == delivery_id)
            .values(
                sent_at=datetime.now(UTC),
                response_code=response_code,
                response_excerpt=excerpt,
            )
        )

    async def mark_failed(
        self, *, delivery_id: int, response_code: int, response_excerpt: str
    ) -> None:
        """Finalise a claim after a non-retriable 4xx (or 410) response.

        Distinct from :meth:`mark_sent` only by the absence of business
        success — both write ``sent_at = now()`` because the column
        marks "the dispatcher reached a terminal decision on this row".
        ``response_code`` and ``response_excerpt`` are still recorded so
        operators can introspect the failure post-hoc.
        """
        excerpt = (response_excerpt or "")[:500]
        await self._s.execute(
            update(WebhookDelivery)
            .where(WebhookDelivery.id == delivery_id)
            .values(
                sent_at=datetime.now(UTC),
                response_code=response_code,
                response_excerpt=excerpt,
            )
        )

    async def rollback(self, *, delivery_id: int) -> None:
        """``DELETE`` a previously-claimed row so the recovery scan can
        re-pick it up.

        Used only for transient errors (network, 5xx, 408, 429) per
        ADR-0023 §3.4 step 11.
        """
        await self._s.execute(delete(WebhookDelivery).where(WebhookDelivery.id == delivery_id))

    # --- Reads ------------------------------------------------------------

    async def list_tags_for_team(self, *, message_id: int, group_id: int) -> list[WebhookTeamTag]:
        """Aggregate tag rows for a message across a whole team (ADR-0023
        §3.2 second SELECT).

        Returns one row per distinct tag id, ordered by name for stable
        receiver-side processing.
        """
        stmt = text(
            """
            SELECT DISTINCT t.id, t.name, t.color
            FROM   message_tags mt
            JOIN   tags t ON t.id = mt.tag_id
            JOIN   users u ON u.id = t.user_id
            JOIN   mail_accounts ma ON ma.id = (
                       SELECT m_inner.mail_account_id
                       FROM   messages m_inner
                       WHERE  m_inner.id = :message_id
                   )
            WHERE  mt.message_id = :message_id
              AND  (u.group_id = :group_id OR u.id = ma.user_id)
              -- round-28: NO ``u.role = 'super_admin'``. The name/color of a
              -- super_admin's personal tag must not leak into another team's
              -- external payload. The ``u.id = ma.user_id`` arm keeps the
              -- mailbox owner's tags even if the owner is outside the group
              -- (defensive). See ADR-0023 §3.2.
            ORDER  BY t.name
            """
        )
        result = await self._s.execute(stmt, {"message_id": message_id, "group_id": group_id})
        return [
            WebhookTeamTag(id=int(row.id), name=str(row.name), color=str(row.color))
            for row in result
        ]

    async def list_missing_for_recovery(self, *, window_hours: int, limit: int) -> list[int]:
        """SQL from ADR-0023 §3.5 — recovery scan.

        Returns ``message_id`` values that:

        - were fetched within the lookback window
          (``WEBHOOK_RECOVERY_WINDOW_HOURS``);
        - have at least one **team** tag applied (group member or mailbox
          owner — NOT a super_admin personal tag; round-28, ADR-0023 §3.2);
        - belong to a team whose webhook is active and not dead;
        - have NO ``webhook_deliveries`` row at all (the dispatcher hadn't
          claimed them yet — either because the worker crashed between
          LPUSH and LPOP, or because a transient failure rolled the row
          back without a re-enqueue).

        Symmetric to TG's :meth:`TelegramNotificationsRepo.list_missing_for_recovery`
        but adds the history-flood filter ``m.internal_date >=
        w.created_at`` per ADR-0023 §1.1.
        """
        cutoff = datetime.now(UTC) - timedelta(hours=window_hours)
        stmt = text(
            """
            SELECT m.id
            FROM   messages m
            JOIN   mail_accounts ma ON ma.id = m.mail_account_id
            WHERE  m.fetched_at > :cutoff
              AND  EXISTS (
                       SELECT 1 FROM message_tags mt
                       JOIN   tags t ON t.id = mt.tag_id
                       JOIN   users u ON u.id = t.user_id
                       WHERE  mt.message_id = m.id
                         AND  (u.group_id = ma.group_id OR u.id = ma.user_id)
                       -- round-28: same team-scoped predicate as
                       -- ``find_active_for_message``. Without it, a message
                       -- tagged ONLY by a super_admin personal tag would pass
                       -- this pre-filter, get re-enqueued hourly for 24h and
                       -- be silently dropped in dispatch (churn). See
                       -- ADR-0023 §3.5 / §3.2.
                   )
              AND  EXISTS (
                       SELECT 1 FROM webhooks w
                       WHERE  w.group_id = ma.group_id
                         AND  w.is_active = TRUE
                         AND  w.dead_at IS NULL
                         AND  m.internal_date >= w.created_at
                   )
              AND  NOT EXISTS (
                       SELECT 1 FROM webhook_deliveries wd
                       JOIN   webhooks w ON w.id = wd.webhook_id
                       WHERE  wd.message_id = m.id
                         AND  w.group_id    = ma.group_id
                   )
            ORDER  BY m.id
            LIMIT  :limit
            """
        )
        result = await self._s.execute(stmt, {"cutoff": cutoff, "limit": int(limit)})
        return [int(row.id) for row in result]
