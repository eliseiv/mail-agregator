"""MessageService — list/read inbox, mark-read, attachment streaming.

Cursor encoding (``docs/04-api-contracts.md``)::

    cursor = base64url(f"{internal_date_iso}|{id}")

Uses ``urlsafe_b64encode`` so the cursor is safe in query strings.

Visibility (ADR-0019 §7.2): super_admin sees all messages, group leaders
and members see every message of every member of their group.
"""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.accounts.schemas import OwnerBriefDTO
from backend.app.deps import VisibilityScope
from backend.app.exceptions import ForbiddenError, NotFoundError, ValidationError
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
from backend.app.repositories.users import UsersRepo
from backend.app.tags.schemas import TagBriefDTO
from shared.logging import get_logger
from shared.models import Attachment, Tag, User
from shared.storage import Storage, get_storage

log = get_logger(__name__)

CURSOR_SEP = "|"


def _encode_cursor(internal_date: datetime, msg_id: int) -> str:
    raw = f"{internal_date.isoformat()}{CURSOR_SEP}{msg_id}".encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _decode_cursor(cursor: str) -> tuple[datetime, int]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(padded.encode()).decode("utf-8")
        date_str, id_str = raw.split(CURSOR_SEP, 1)
        return datetime.fromisoformat(date_str), int(id_str)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValidationError("Invalid pagination cursor", field="cursor") from exc


def _to_tag_brief(tag: Tag) -> TagBriefDTO:
    return TagBriefDTO(id=tag.id, name=tag.name, color=tag.color)


def _owner_brief(u: User) -> OwnerBriefDTO:
    return OwnerBriefDTO(id=u.id, username=u.username, display_name=u.display_name)


class MessageService:
    def __init__(self, session: AsyncSession) -> None:
        self._db = session
        self._repo = MessagesRepo(session)
        self._accounts = MailAccountsRepo(session)
        self._tags = MessageTagsRepo(session)
        self._users = UsersRepo(session)
        self._storage: Storage = get_storage()

    # --- Visibility helpers ------------------------------------------------

    async def visible_user_ids(
        self, scope: VisibilityScope, *, group_id: int | None = None
    ) -> list[int] | None:
        """Resolve the visibility filter as a list of mailbox-owner user_ids.

        ``group_id`` (super-admin only) restricts the listing to one group.
        Non-admin callers pass ``group_id=None`` and always see only their
        own group.
        """
        if scope.is_super_admin:
            if group_id is None:
                return None
            return await self._users.list_user_ids_in_group(group_id)
        if group_id is not None and group_id != scope.group_id:
            raise ForbiddenError("user_not_in_group_scope")
        if scope.group_id is None:
            return []
        return await self._users.list_user_ids_in_group(scope.group_id)

    # --- List --------------------------------------------------------------

    async def list_for_scope(
        self,
        scope: VisibilityScope,
        *,
        account_id: int | None,
        unread: bool | None,
        cursor: str | None,
        limit: int,
        tag_id: int | None = None,
        group_id: int | None = None,
    ) -> MessageListResponse:
        cursor_date: datetime | None = None
        cursor_id: int | None = None
        if cursor:
            cursor_date, cursor_id = _decode_cursor(cursor)

        # Tag ownership: per-user — owner of the tag is the caller, not
        # the mailbox owner. ADR-0019 §7.4.
        if tag_id is not None and not await self._repo.is_tag_owned(
            tag_id=tag_id, user_id=scope.user_id
        ):
            raise NotFoundError()

        visible = await self.visible_user_ids(scope, group_id=group_id)

        rows = await self._repo.list_for_user_ids(
            user_ids=visible,
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
        # Tag list still per-mailbox-owner (ADR-0019 §7.4: leader sees
        # tags of the message owner, not of themselves).
        owner_user_ids: list[int] = sorted({a.user_id for _, a in rows})
        owner_map = await self._users.get_many_by_ids(owner_user_ids)
        tags_map = await self._tags.list_for_messages_bulk(ids)

        items: list[MessageListItem] = []
        for m, a in rows:
            owner_user = owner_map.get(a.user_id)
            if owner_user is None:
                continue  # FK should prevent it
            items.append(
                MessageListItem(
                    id=m.id,
                    mail_account_id=m.mail_account_id,
                    mail_account_email=a.email,
                    mail_account_display_name=a.display_name,
                    owner=_owner_brief(owner_user),
                    from_addr=m.from_addr,
                    from_name=m.from_name,
                    subject=m.subject,
                    internal_date=m.internal_date,
                    is_read=m.is_read,
                    has_attachments=att_map.get(m.id, False),
                    tags=[_to_tag_brief(t) for t in tags_map.get(m.id, [])],
                )
            )
        return MessageListResponse(items=items, next_cursor=next_cursor)

    # --- Get ---------------------------------------------------------------

    async def get(self, *, scope: VisibilityScope, message_id: int) -> MessageDetail:
        visible = await self.visible_user_ids(scope)
        msg = await self._repo.get_for_user_ids(message_id=message_id, user_ids=visible)
        if msg is None:
            raise NotFoundError()
        acc = await self._accounts.get_by_id(msg.mail_account_id)
        assert acc is not None  # FK guarantees this
        owner_user = await self._users.get_by_id(acc.user_id)
        assert owner_user is not None
        atts_map = await self._repo.list_attachments_bulk([msg.id])
        atts: list[Attachment] = atts_map.get(msg.id, [])
        tags = await self._tags.list_for_message(msg.id)
        return MessageDetail(
            id=msg.id,
            mail_account_id=msg.mail_account_id,
            mail_account_email=acc.email,
            mail_account_display_name=acc.display_name,
            owner=_owner_brief(owner_user),
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

    # --- Mutations ---------------------------------------------------------

    async def mark_read(
        self,
        *,
        scope: VisibilityScope,
        message_id: int,
        payload: MarkReadRequest,
    ) -> None:
        visible = await self.visible_user_ids(scope)
        msg = await self._repo.get_for_user_ids(message_id=message_id, user_ids=visible)
        if msg is None:
            raise NotFoundError()
        if msg.is_read == payload.is_read:
            return  # idempotent no-op
        await self._repo.mark_read(message_id=message_id, is_read=payload.is_read)

    async def stream_attachment(
        self,
        *,
        scope: VisibilityScope,
        message_id: int,
        attachment_id: int,
    ) -> tuple[Attachment, AsyncIterator[bytes]]:
        visible = await self.visible_user_ids(scope)
        att = await self._repo.get_attachment_for_user_ids(
            attachment_id=attachment_id,
            message_id=message_id,
            user_ids=visible,
        )
        if att is None or att.skipped_too_large:
            raise NotFoundError()
        stream: AsyncIterator[bytes] = self._storage.get_object_stream(att.s3_key)
        return att, stream

    # --- Unread counter ----------------------------------------------------

    async def count_unread_for_scope(self, scope: VisibilityScope) -> int:
        visible = await self.visible_user_ids(scope)
        return await self._repo.count_unread_for_user_ids(visible)
