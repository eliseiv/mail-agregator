"""Repository for ``messages`` (the connector's push-outbox, ADR-0043 §1).

ADR-0044 §4 (phase A3, KEEP-repository detach): the DROP-ORM imports
(``Attachment`` / ``MessageTag`` / ``Tag`` / ``UserGroup``) and every method
that used them are gone — tag filters (``list_for_user*``, ``is_tag_owned``,
``is_tag_visible_to_scope``), the attachment methods (including the only raw
SQL ``nextval('attachments_id_seq')``, §9 caveat B) and the HTML inbox helpers.

What survives is what the connector actually runs:

- ``get_for_user_ids`` — resolve the original message for the external reply
  (ADR-0035);
- ``list_since_id`` / ``list_before_id`` — the external PULL (ADR-0029/0036);
- ``insert_message_idempotent`` — the sync insert (ADR-0008);
- ``list_for_crm_push`` / ``mark_pushed`` / ``list_pending_push`` — the CRM
  push-outbox (ADR-0043 §2);
- ``select_expired`` / ``delete_messages`` — retention (ADR-0011).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models import MailAccount, Message


class MessagesRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    # --- Reads -------------------------------------------------------------

    async def get_for_user_ids(
        self,
        *,
        message_id: int,
        mail_account_ids: list[int] | None,
    ) -> Message | None:
        """Scope-aware get.

        ``mail_account_ids=None`` = "no scope filter"; ``[]`` = nothing visible
        (returns ``None``). The external reply passes the canonical mailbox set
        (ADR-0029 §5), so a caller can only reply to a message it could have
        pulled.
        """
        if mail_account_ids is None:
            return await self._s.get(Message, message_id)
        if not mail_account_ids:
            return None
        stmt = select(Message).where(
            Message.id == message_id,
            Message.mail_account_id.in_(mail_account_ids),
        )
        return (await self._s.execute(stmt)).scalar_one_or_none()

    async def list_since_id(
        self,
        *,
        mail_account_ids: list[int],
        since_id: int,
        limit: int,
    ) -> list[tuple[Message, MailAccount]]:
        """Keyset listing for the external PULL-API (ADR-0029 §1).

        Returns ``[(Message, MailAccount)]`` for messages whose ``id`` is
        strictly greater than ``since_id`` and whose ``mail_account_id`` is in
        ``mail_account_ids`` (the canonical-deduped set — see
        :meth:`MailAccountsRepo.list_canonical_account_ids`), ordered by
        ``messages.id ASC`` and capped at ``limit``. The monotonic
        ``messages.id BIGSERIAL`` keyset guarantees no gaps/dupes in the
        cursor between successive pages.

        ``mail_account_ids=[]`` (the system has no mailboxes at all) returns
        ``[]`` WITHOUT issuing a query. The ``IN`` is parameterised.
        """
        if not mail_account_ids:
            return []
        stmt = (
            select(Message, MailAccount)
            .join(MailAccount, MailAccount.id == Message.mail_account_id)
            .where(
                Message.id > since_id,
                Message.mail_account_id.in_(mail_account_ids),
            )
            .order_by(Message.id.asc())
            .limit(limit)
        )
        rows = (await self._s.execute(stmt)).all()
        return [(row[0], row[1]) for row in rows]

    async def list_before_id(
        self,
        *,
        mail_account_ids: list[int],
        before_id: int | None,
        limit: int,
    ) -> list[tuple[Message, MailAccount]]:
        """Backward / newest-first keyset listing for the external API (ADR-0036 §2).

        The mirror of :meth:`list_since_id`: same canonical-scope filter and the
        same monotonic ``messages.id`` keyset, only reversed:

        - ``before_id is None`` → **latest** page: the freshest ``limit`` rows
          (``ORDER BY id DESC LIMIT limit``), no lower id bound.
        - ``before_id`` set → **older** page: rows with ``id < before_id``.

        Reverse-scan over the ``messages.id`` PK — no new index/migration
        (ADR-0036 §2). ``mail_account_ids=[]`` returns ``[]`` WITHOUT issuing a
        query; the ``IN`` and ``id <`` bounds are parameterised.
        """
        if not mail_account_ids:
            return []
        stmt = select(Message, MailAccount).join(
            MailAccount, MailAccount.id == Message.mail_account_id
        )
        if before_id is not None:
            stmt = stmt.where(Message.id < before_id)
        stmt = (
            stmt.where(Message.mail_account_id.in_(mail_account_ids))
            .order_by(Message.id.desc())
            .limit(limit)
        )
        rows = (await self._s.execute(stmt)).all()
        return [(row[0], row[1]) for row in rows]

    # --- Writes ------------------------------------------------------------

    async def insert_message_idempotent(
        self,
        *,
        mail_account_id: int,
        uid: int,
        uidvalidity: int,
        message_id_header: str | None,
        from_addr: str,
        from_name: str | None,
        to_addrs: str,
        cc_addrs: str | None,
        subject: str | None,
        internal_date: datetime,
        body_text: str,
        body_html: str | None,
        body_truncated: bool,
        body_present: bool,
        in_reply_to: str | None,
        refs_header: str | None,
    ) -> int | None:
        """``ON CONFLICT DO NOTHING`` insert. Returns the new id or None on conflict.

        See ADR-0008 (idempotency invariant). ``body_html`` is the sanitised
        HTML body (NULL when the email has no ``text/html`` part).
        """
        stmt = (
            pg_insert(Message)
            .values(
                mail_account_id=mail_account_id,
                uid=uid,
                uidvalidity=uidvalidity,
                message_id_header=message_id_header,
                from_addr=from_addr,
                from_name=from_name,
                to_addrs=to_addrs,
                cc_addrs=cc_addrs,
                subject=subject,
                internal_date=internal_date,
                body_text=body_text,
                body_html=body_html,
                body_truncated=body_truncated,
                body_present=body_present,
                in_reply_to=in_reply_to,
                refs_header=refs_header,
            )
            .on_conflict_do_nothing(index_elements=["mail_account_id", "uidvalidity", "uid"])
            .returning(Message.id)
        )
        row = (await self._s.execute(stmt)).one_or_none()
        return int(row[0]) if row else None

    # --- CRM push-outbox (ADR-0043 §2, used by worker.crm_push_*) ----------

    async def list_for_crm_push(self, message_ids: list[int]) -> list[Message]:
        """Bulk-load ``messages`` by id for the CRM ingest payload.

        Rows are returned regardless of ``pushed_at`` (a re-enqueued /
        recovered id may already be marked; the CRM ingest is idempotent, so a
        redundant push is harmless). Ordered by ``id`` for stable batching.
        Missing ids are simply absent from the result.
        """
        if not message_ids:
            return []
        stmt = select(Message).where(Message.id.in_(message_ids)).order_by(Message.id)
        return list((await self._s.execute(stmt)).scalars().all())

    async def mark_pushed(self, message_ids: list[int]) -> int:
        """Stamp ``pushed_at = now()`` for delivered messages (guarded).

        Only rows still ``pushed_at IS NULL`` are updated (idempotent — a
        double delivery does not move the timestamp). Returns the number of
        rows transitioned.
        """
        if not message_ids:
            return 0
        stmt = (
            update(Message)
            .where(Message.id.in_(message_ids), Message.pushed_at.is_(None))
            .values(pushed_at=func.now())
        )
        result = await self._s.execute(stmt)
        return int(result.rowcount or 0)

    async def list_pending_push(self, *, window_start: datetime, limit: int) -> list[int]:
        """``messages.id`` not yet delivered to the CRM (recovery scan).

        Returns ids with ``pushed_at IS NULL`` fetched within the lookback
        window (``fetched_at > window_start``), oldest first, capped at
        ``limit``. Uses the ``ix_messages_pushed_at_pending`` partial index.
        """
        stmt = (
            select(Message.id)
            .where(
                Message.pushed_at.is_(None),
                Message.fetched_at > window_start,
            )
            .order_by(Message.id)
            .limit(limit)
        )
        return [int(r[0]) for r in (await self._s.execute(stmt)).all()]

    # --- Retention / deletion (used by worker.cleanup) --------------------

    async def select_expired(self, threshold: datetime, limit: int) -> list[tuple[int, int, int]]:
        """Return list of ``(message_id, mail_account_id, user_id)`` for old rows."""
        stmt = (
            select(Message.id, Message.mail_account_id, MailAccount.user_id)
            .join(MailAccount, MailAccount.id == Message.mail_account_id)
            .where(Message.internal_date < threshold)
            .order_by(Message.id)
            .limit(limit)
        )
        rows = (await self._s.execute(stmt)).all()
        return [(int(r[0]), int(r[1]), int(r[2])) for r in rows]

    async def delete_messages(self, message_ids: list[int]) -> int:
        if not message_ids:
            return 0
        stmt = delete(Message).where(Message.id.in_(message_ids))
        result = await self._s.execute(stmt)
        return int(result.rowcount or 0)
