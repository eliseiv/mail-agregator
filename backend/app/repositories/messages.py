"""Repository for ``messages`` and ``attachments``.

Cursor-based pagination per ``docs/04-api-contracts.md`` (keyset by
``(internal_date DESC, id DESC)``). Cursor encoding is in
:mod:`backend.app.messages.service`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import and_, delete, exists, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models import Attachment, MailAccount, Message, MessageTag, Tag


class MessagesRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    # --- Reads -------------------------------------------------------------

    async def get_owned(self, *, message_id: int, user_id: int) -> Message | None:
        """Return ``message_id`` only if it belongs to ``user_id``."""
        stmt = (
            select(Message)
            .join(MailAccount, MailAccount.id == Message.mail_account_id)
            .where(Message.id == message_id, MailAccount.user_id == user_id)
        )
        return (await self._s.execute(stmt)).scalar_one_or_none()

    async def get_for_user_ids(
        self,
        *,
        message_id: int,
        user_ids: list[int] | None,
    ) -> Message | None:
        """Visibility-aware get.

        ``user_ids=None`` = "no scope filter" (super-admin path).
        ``user_ids=[]`` = "no users visible" — returns ``None``.
        """
        if user_ids is None:
            return await self._s.get(Message, message_id)
        if not user_ids:
            return None
        stmt = (
            select(Message)
            .join(MailAccount, MailAccount.id == Message.mail_account_id)
            .where(Message.id == message_id, MailAccount.user_id.in_(user_ids))
        )
        return (await self._s.execute(stmt)).scalar_one_or_none()

    async def list_for_user(
        self,
        *,
        user_id: int,
        account_id: int | None,
        tag_id: int | None = None,
        unread: bool | None,
        cursor_internal_date: datetime | None,
        cursor_id: int | None,
        limit: int,
    ) -> list[tuple[Message, str]]:
        """Single-user list (legacy path retained for tags-tagging logic).

        See :meth:`list_for_user_ids` for the visibility-aware version
        used by ``GET /api/messages``.
        """
        stmt = (
            select(Message, MailAccount.email)
            .join(MailAccount, MailAccount.id == Message.mail_account_id)
            .where(MailAccount.user_id == user_id)
        )
        if account_id is not None:
            stmt = stmt.where(Message.mail_account_id == account_id)
        if tag_id is not None:
            stmt = stmt.join(
                MessageTag,
                and_(
                    MessageTag.message_id == Message.id,
                    MessageTag.tag_id == tag_id,
                ),
            )
        if unread is True:
            stmt = stmt.where(Message.is_read.is_(False))
        elif unread is False:
            stmt = stmt.where(Message.is_read.is_(True))
        if cursor_internal_date is not None and cursor_id is not None:
            # Strict keyset: rows strictly older OR same date but smaller id.
            stmt = stmt.where(
                or_(
                    Message.internal_date < cursor_internal_date,
                    and_(
                        Message.internal_date == cursor_internal_date,
                        Message.id < cursor_id,
                    ),
                )
            )
        stmt = stmt.order_by(Message.internal_date.desc(), Message.id.desc()).limit(limit)
        rows = (await self._s.execute(stmt)).all()
        return [(row[0], row[1]) for row in rows]

    async def list_for_user_ids(
        self,
        *,
        user_ids: list[int] | None,
        account_id: int | None,
        tag_id: int | None = None,
        unread: bool | None,
        cursor_internal_date: datetime | None,
        cursor_id: int | None,
        limit: int,
    ) -> list[tuple[Message, MailAccount]]:
        """Visibility-aware listing.

        ``user_ids=None`` = no filter (super-admin); ``user_ids=[]`` =
        empty result. Returns ``[(Message, MailAccount)]`` so the caller
        can build the per-row ``mail_account_display_name`` and ``owner``
        without an extra round-trip.
        """
        stmt = select(Message, MailAccount).join(
            MailAccount, MailAccount.id == Message.mail_account_id
        )
        if user_ids is not None:
            if not user_ids:
                return []
            stmt = stmt.where(MailAccount.user_id.in_(user_ids))
        if account_id is not None:
            stmt = stmt.where(Message.mail_account_id == account_id)
        if tag_id is not None:
            stmt = stmt.join(
                MessageTag,
                and_(
                    MessageTag.message_id == Message.id,
                    MessageTag.tag_id == tag_id,
                ),
            )
        if unread is True:
            stmt = stmt.where(Message.is_read.is_(False))
        elif unread is False:
            stmt = stmt.where(Message.is_read.is_(True))
        if cursor_internal_date is not None and cursor_id is not None:
            stmt = stmt.where(
                or_(
                    Message.internal_date < cursor_internal_date,
                    and_(
                        Message.internal_date == cursor_internal_date,
                        Message.id < cursor_id,
                    ),
                )
            )
        stmt = stmt.order_by(Message.internal_date.desc(), Message.id.desc()).limit(limit)
        rows = (await self._s.execute(stmt)).all()
        return [(row[0], row[1]) for row in rows]

    async def count_unread_for_user_ids(self, user_ids: list[int] | None) -> int:
        stmt = (
            select(func.count(Message.id))
            .join(MailAccount, MailAccount.id == Message.mail_account_id)
            .where(Message.is_read.is_(False))
        )
        if user_ids is not None:
            if not user_ids:
                return 0
            stmt = stmt.where(MailAccount.user_id.in_(user_ids))
        return int((await self._s.execute(stmt)).scalar_one())

    async def is_tag_owned(self, *, tag_id: int, user_id: int) -> bool:
        """Return True iff the tag exists and belongs to ``user_id``.

        Used to gate ``GET /api/messages?tag_id=X``: foreign / unknown
        tags must surface as 404 (per ADR-0017 §9 — never leak existence).
        """
        stmt = select(exists().where(Tag.id == tag_id, Tag.user_id == user_id))
        return bool((await self._s.execute(stmt)).scalar_one())

    async def list_attachments_bulk(self, message_ids: list[int]) -> dict[int, list[Attachment]]:
        if not message_ids:
            return {}
        stmt = (
            select(Attachment)
            .where(Attachment.message_id.in_(message_ids))
            .order_by(Attachment.message_id, Attachment.id)
        )
        out: dict[int, list[Attachment]] = {mid: [] for mid in message_ids}
        for att in (await self._s.execute(stmt)).scalars():
            out[att.message_id].append(att)
        return out

    async def has_attachments_bulk(self, message_ids: list[int]) -> dict[int, bool]:
        if not message_ids:
            return {}
        stmt = (
            select(
                Attachment.message_id,
                func.count(Attachment.id),
            )
            .where(Attachment.message_id.in_(message_ids))
            .group_by(Attachment.message_id)
        )
        present = {mid: False for mid in message_ids}
        for mid, cnt in (await self._s.execute(stmt)).all():
            present[mid] = cnt > 0
        return present

    async def get_attachment_owned(
        self, *, attachment_id: int, message_id: int, user_id: int
    ) -> Attachment | None:
        stmt = (
            select(Attachment)
            .join(Message, Message.id == Attachment.message_id)
            .join(MailAccount, MailAccount.id == Message.mail_account_id)
            .where(
                Attachment.id == attachment_id,
                Attachment.message_id == message_id,
                MailAccount.user_id == user_id,
            )
        )
        return (await self._s.execute(stmt)).scalar_one_or_none()

    async def get_attachment_for_user_ids(
        self,
        *,
        attachment_id: int,
        message_id: int,
        user_ids: list[int] | None,
    ) -> Attachment | None:
        """Visibility-aware get for ``GET /api/messages/{id}/attachments/{aid}``.

        ``user_ids=None`` = super-admin (no filter); ``user_ids=[]`` =
        empty result.
        """
        if user_ids is None:
            stmt = (
                select(Attachment)
                .join(Message, Message.id == Attachment.message_id)
                .where(
                    Attachment.id == attachment_id,
                    Attachment.message_id == message_id,
                )
            )
            return (await self._s.execute(stmt)).scalar_one_or_none()
        if not user_ids:
            return None
        stmt = (
            select(Attachment)
            .join(Message, Message.id == Attachment.message_id)
            .join(MailAccount, MailAccount.id == Message.mail_account_id)
            .where(
                Attachment.id == attachment_id,
                Attachment.message_id == message_id,
                MailAccount.user_id.in_(user_ids),
            )
        )
        return (await self._s.execute(stmt)).scalar_one_or_none()

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
        body_truncated: bool,
        body_present: bool,
        in_reply_to: str | None,
        refs_header: str | None,
    ) -> int | None:
        """``ON CONFLICT DO NOTHING`` insert. Returns the new id or None on conflict.

        See ADR-0008 (idempotency invariant).
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

    async def insert_attachment(
        self,
        *,
        message_id: int,
        filename: str,
        content_type: str | None,
        size_bytes: int,
        s3_key: str,
        skipped_too_large: bool,
    ) -> int:
        att = Attachment(
            message_id=message_id,
            filename=filename,
            content_type=content_type,
            size_bytes=size_bytes,
            s3_key=s3_key,
            skipped_too_large=skipped_too_large,
        )
        self._s.add(att)
        await self._s.flush()
        return att.id

    async def reserve_attachment_id(self) -> int:
        """``SELECT nextval('attachments_id_seq')``.

        Used to build the S3 key (which embeds ``attachment_id``) before the
        actual INSERT — same pattern as :meth:`MailAccountsRepo.next_account_id`.
        """
        row = await self._s.execute(text("SELECT nextval('attachments_id_seq')"))
        return int(row.scalar_one())

    async def insert_attachment_with_id(
        self,
        *,
        attachment_id: int,
        message_id: int,
        filename: str,
        content_type: str | None,
        size_bytes: int,
        s3_key: str,
        skipped_too_large: bool,
    ) -> None:
        att = Attachment(
            id=attachment_id,
            message_id=message_id,
            filename=filename,
            content_type=content_type,
            size_bytes=size_bytes,
            s3_key=s3_key,
            skipped_too_large=skipped_too_large,
        )
        self._s.add(att)
        await self._s.flush()

    async def mark_read(self, *, message_id: int, is_read: bool) -> None:
        await self._s.execute(
            update(Message).where(Message.id == message_id).values(is_read=is_read)
        )

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

    async def select_attachment_keys_for_messages(self, message_ids: list[int]) -> list[str]:
        if not message_ids:
            return []
        stmt = select(Attachment.s3_key).where(
            Attachment.message_id.in_(message_ids),
            Attachment.skipped_too_large.is_(False),
        )
        return [str(r[0]) for r in (await self._s.execute(stmt)).all()]

    async def delete_messages(self, message_ids: list[int]) -> int:
        if not message_ids:
            return 0
        stmt = delete(Message).where(Message.id.in_(message_ids))
        result = await self._s.execute(stmt)
        return int(result.rowcount or 0)

    # --- Cleanup helpers for cascading user/account delete ----------------

    async def select_attachment_keys_for_user(self, user_id: int) -> list[str]:
        stmt = (
            select(Attachment.s3_key)
            .join(Message, Message.id == Attachment.message_id)
            .join(MailAccount, MailAccount.id == Message.mail_account_id)
            .where(
                MailAccount.user_id == user_id,
                Attachment.skipped_too_large.is_(False),
            )
        )
        return [str(r[0]) for r in (await self._s.execute(stmt)).all()]

    async def select_attachment_keys_for_account(self, account_id: int) -> list[str]:
        stmt = (
            select(Attachment.s3_key)
            .join(Message, Message.id == Attachment.message_id)
            .where(
                Message.mail_account_id == account_id,
                Attachment.skipped_too_large.is_(False),
            )
        )
        return [str(r[0]) for r in (await self._s.execute(stmt)).all()]

    # --- Stats for admin / delete-user response ---------------------------

    async def stats_for_user(self, user_id: int) -> tuple[int, int, int]:
        """Return ``(messages, attachments, mail_accounts)`` for ``user_id``."""
        msgs = (
            await self._s.execute(
                select(func.count(Message.id))
                .join(MailAccount, MailAccount.id == Message.mail_account_id)
                .where(MailAccount.user_id == user_id)
            )
        ).scalar_one()
        atts = (
            await self._s.execute(
                select(func.count(Attachment.id))
                .join(Message, Message.id == Attachment.message_id)
                .join(MailAccount, MailAccount.id == Message.mail_account_id)
                .where(MailAccount.user_id == user_id)
            )
        ).scalar_one()
        accs = (
            await self._s.execute(
                select(func.count(MailAccount.id)).where(MailAccount.user_id == user_id)
            )
        ).scalar_one()
        return int(msgs), int(atts), int(accs)

    async def has_any_attachments(self, message_id: int) -> bool:
        stmt = select(exists().where(Attachment.message_id == message_id))
        return bool((await self._s.execute(stmt)).scalar_one())

    async def count_unread_for_user(self, user_id: int) -> int:
        stmt = (
            select(func.count(Message.id))
            .join(MailAccount, MailAccount.id == Message.mail_account_id)
            .where(MailAccount.user_id == user_id, Message.is_read.is_(False))
        )
        return int((await self._s.execute(stmt)).scalar_one())

    async def list_for_user_html(
        self,
        *,
        user_id: int,
        account_id: int | None,
        limit: int,
        tag_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """Convenience for HTML inbox: list with ``has_attachments`` precomputed."""
        rows = await self.list_for_user(
            user_id=user_id,
            account_id=account_id,
            tag_id=tag_id,
            unread=None,
            cursor_internal_date=None,
            cursor_id=None,
            limit=limit,
        )
        ids = [m.id for m, _ in rows]
        att_map = await self.has_attachments_bulk(ids)
        return [
            {
                "message": m,
                "account_email": email,
                "has_attachments": att_map.get(m.id, False),
            }
            for m, email in rows
        ]
