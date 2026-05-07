"""MessageService — list/read inbox, mark-read, attachment streaming.

Cursor encoding (``docs/04-api-contracts.md``)::

    cursor = base64url(f"{internal_date_iso}|{id}")

Uses ``urlsafe_b64encode`` so the cursor is safe in query strings.
"""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.exceptions import NotFoundError, ValidationError
from backend.app.messages.schemas import (
    AttachmentDTO,
    MarkReadRequest,
    MessageDetail,
    MessageListItem,
    MessageListResponse,
)
from backend.app.repositories.mail_accounts import MailAccountsRepo
from backend.app.repositories.messages import MessagesRepo
from backend.app.repositories.tags import MessageTagsRepo
from backend.app.tags.schemas import TagBriefDTO
from shared.logging import get_logger
from shared.models import Attachment, Tag
from shared.storage import Storage, get_storage

log = get_logger(__name__)

CURSOR_SEP = "|"


def _encode_cursor(internal_date: datetime, msg_id: int) -> str:
    raw = f"{internal_date.isoformat()}{CURSOR_SEP}{msg_id}".encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _decode_cursor(cursor: str) -> tuple[datetime, int]:
    try:
        # Re-pad for base64 decode.
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(padded.encode()).decode("utf-8")
        date_str, id_str = raw.split(CURSOR_SEP, 1)
        return datetime.fromisoformat(date_str), int(id_str)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValidationError("Invalid pagination cursor", field="cursor") from exc


def _to_tag_brief(tag: Tag) -> TagBriefDTO:
    return TagBriefDTO(id=tag.id, name=tag.name, color=tag.color)


class MessageService:
    def __init__(self, session: AsyncSession) -> None:
        self._db = session
        self._repo = MessagesRepo(session)
        self._accounts = MailAccountsRepo(session)
        self._tags = MessageTagsRepo(session)
        self._storage: Storage = get_storage()

    async def list_for_user(
        self,
        *,
        user_id: int,
        account_id: int | None,
        unread: bool | None,
        cursor: str | None,
        limit: int,
        tag_id: int | None = None,
    ) -> MessageListResponse:
        cursor_date: datetime | None = None
        cursor_id: int | None = None
        if cursor:
            cursor_date, cursor_id = _decode_cursor(cursor)

        # Validate tag ownership before any heavy SELECT — leaks nothing
        # about other users' tags (ADR-0017 §9 — 404 on foreign tag_id).
        if tag_id is not None and not await self._repo.is_tag_owned(tag_id=tag_id, user_id=user_id):
            raise NotFoundError()

        # Fetch one extra row so we know whether to emit ``next_cursor``.
        rows = await self._repo.list_for_user(
            user_id=user_id,
            account_id=account_id,
            tag_id=tag_id,
            unread=unread,
            cursor_internal_date=cursor_date,
            cursor_id=cursor_id,
            limit=limit + 1,
        )

        next_cursor: str | None = None
        if len(rows) > limit:
            tail = rows[limit - 1][0]
            next_cursor = _encode_cursor(tail.internal_date, tail.id)
            rows = rows[:limit]

        ids = [m.id for m, _ in rows]
        att_map = await self._repo.has_attachments_bulk(ids)
        tags_map = await self._tags.list_for_messages_bulk(ids)

        items = [
            MessageListItem(
                id=m.id,
                mail_account_id=m.mail_account_id,
                mail_account_email=email,
                from_addr=m.from_addr,
                from_name=m.from_name,
                subject=m.subject,
                internal_date=m.internal_date,
                is_read=m.is_read,
                has_attachments=att_map.get(m.id, False),
                tags=[_to_tag_brief(t) for t in tags_map.get(m.id, [])],
            )
            for m, email in rows
        ]
        return MessageListResponse(items=items, next_cursor=next_cursor)

    async def get(self, *, user_id: int, message_id: int) -> MessageDetail:
        msg = await self._repo.get_owned(message_id=message_id, user_id=user_id)
        if msg is None:
            raise NotFoundError()

        # Owner email — small extra query, much clearer than joining in the row.
        acc = await self._accounts.get_by_id(msg.mail_account_id)
        assert acc is not None  # FK guarantees this
        atts_map = await self._repo.list_attachments_bulk([msg.id])
        atts: list[Attachment] = atts_map.get(msg.id, [])
        tags = await self._tags.list_for_message(msg.id)

        return MessageDetail(
            id=msg.id,
            mail_account_id=msg.mail_account_id,
            mail_account_email=acc.email,
            from_addr=msg.from_addr,
            from_name=msg.from_name,
            to_addrs=msg.to_addrs,
            cc_addrs=msg.cc_addrs,
            subject=msg.subject,
            internal_date=msg.internal_date,
            body_text=msg.body_text,
            body_truncated=msg.body_truncated,
            body_present=msg.body_present,
            in_reply_to=msg.in_reply_to,
            is_read=msg.is_read,
            attachments=[
                AttachmentDTO(
                    id=a.id,
                    filename=a.filename,
                    content_type=a.content_type,
                    size_bytes=a.size_bytes,
                    skipped_too_large=a.skipped_too_large,
                )
                for a in atts
            ],
            tags=[_to_tag_brief(t) for t in tags],
        )

    async def mark_read(
        self,
        *,
        user_id: int,
        message_id: int,
        payload: MarkReadRequest,
    ) -> None:
        msg = await self._repo.get_owned(message_id=message_id, user_id=user_id)
        if msg is None:
            raise NotFoundError()
        if msg.is_read == payload.is_read:
            return  # idempotent no-op
        await self._repo.mark_read(message_id=message_id, is_read=payload.is_read)

    async def stream_attachment(
        self,
        *,
        user_id: int,
        message_id: int,
        attachment_id: int,
    ) -> tuple[Attachment, AsyncIterator[bytes]]:
        att = await self._repo.get_attachment_owned(
            attachment_id=attachment_id,
            message_id=message_id,
            user_id=user_id,
        )
        if att is None or att.skipped_too_large:
            # Skipped attachments are surfaced as 404 per data-model contract.
            raise NotFoundError()
        stream: AsyncIterator[bytes] = self._storage.get_object_stream(att.s3_key)
        return att, stream
