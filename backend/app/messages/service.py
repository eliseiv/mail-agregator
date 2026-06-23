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
from shared.html_sanitize import collapse_blank_lines_html, collapse_blank_lines_text
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
        """Resolve the visibility filter as a list of ``mail_accounts.id``.

        FE-FIX round-10: the filter shifted from per-user (``users.group_id``)
        to per-account (``mail_accounts.group_id``) so that moving a user
        between groups no longer reassigns their messages along with them.

        Returns:
            ``None`` — no filter (super-admin path without ``group_id``);
            ``[]``   — no accounts visible to the caller;
            ``[id…]`` — the explicit list of visible ``mail_accounts.id``.

        The method name is preserved to avoid a sweeping rename across
        callers; the *meaning* now is "visible mail-account ids".
        """
        if scope.is_super_admin:
            if group_id is None:
                # Round-18: when two teams added the same mailbox, the system
                # has multiple mail_account.id rows with identical email,
                # each polled independently by the worker -> duplicate
                # messages. For the unscoped super-admin view we collapse
                # to one canonical mail_account.id per LOWER(email), so the
                # Inbox no longer shows duplicates.
                return await self._accounts.list_canonical_account_ids()
            return await self._accounts.list_account_ids_in_group(group_id)
        if group_id is not None:
            # ADR-0030: a non-admin caller may scope the inbox to ANY team
            # they are a member of (home or additional), not only the home
            # team. Restrict visibility to that single requested team (plus
            # the caller's own personal accounts, preserving prior semantics).
            if group_id not in scope.group_ids:
                raise ForbiddenError("user_not_in_group_scope")
            return await self._accounts.list_account_ids_visible(
                group_ids={group_id}, owner_user_id=scope.user_id
            )
        return await self._accounts.list_account_ids_visible(
            group_ids=scope.group_ids, owner_user_id=scope.user_id
        )

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

        # Round-20: widen tag-filter visibility — super_admin may filter
        # by any tag in the system; team members may filter by any tag
        # belonging to themselves OR another member of any of their teams.
        # ADR-0030 §2: pass the full ``group_ids`` set so the tag-filter is
        # consistent with the (multi-group) message visibility — a member of
        # teams [A, B] filtering by a team-B colleague's tag must not 404
        # while the team-B messages it scopes are visible.
        if tag_id is not None and not await self._repo.is_tag_visible_to_scope(
            tag_id=tag_id,
            is_super_admin=scope.is_super_admin,
            user_id=scope.user_id,
            group_ids=scope.group_ids,
        ):
            raise NotFoundError()

        visible = await self.visible_user_ids(scope, group_id=group_id)

        rows = await self._repo.list_for_user_ids(
            mail_account_ids=visible,
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
            # Round-16: dedup tag badges by (name, color). Pre-fix history
            # could land the same email into two mail_accounts (one per team)
            # and apply two team-scoped tags with identical name/color — the
            # UI would then render two indistinguishable chips. Even after
            # the duplicate accounts are removed, defensive dedup here is
            # cheap and protects against any future tag-naming collisions.
            seen_tag_keys: set[tuple[str, str]] = set()
            unique_tags: list[Tag] = []
            for t in tags_map.get(m.id, []):
                key = (t.name, t.color)
                if key in seen_tag_keys:
                    continue
                seen_tag_keys.add(key)
                unique_tags.append(t)
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
                    tags=[_to_tag_brief(t) for t in unique_tags],
                )
            )
        return MessageListResponse(items=items, next_cursor=next_cursor)

    # --- Get ---------------------------------------------------------------

    async def get(self, *, scope: VisibilityScope, message_id: int) -> MessageDetail:
        visible = await self.visible_user_ids(scope)
        msg = await self._repo.get_for_user_ids(message_id=message_id, mail_account_ids=visible)
        if msg is None:
            raise NotFoundError()
        acc = await self._accounts.get_by_id(msg.mail_account_id)
        assert acc is not None  # FK guarantees this
        owner_user = await self._users.get_by_id(acc.user_id)
        assert owner_user is not None
        atts_map = await self._repo.list_attachments_bulk([msg.id])
        atts: list[Attachment] = atts_map.get(msg.id, [])
        # Round-21 (bug #2): mirror the list_for_scope dedup so the admin
        # message detail (and any downstream consumer of MessageDetail.tags)
        # gets one chip per (name, color). Round-15 auto-tagging creates a
        # sibling ``tags`` row per team-member of the mailbox owner, so the
        # raw repo result lists the same logical tag N times.
        raw_tags = await self._tags.list_for_message(msg.id)
        seen_tag_keys: set[tuple[str, str]] = set()
        tags: list[Tag] = []
        for t in raw_tags:
            key = (t.name, t.color)
            if key in seen_tag_keys:
                continue
            seen_tag_keys.add(key)
            tags.append(t)
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
            # Round-37 (ADR-0022 §2.10): normalise the "tall column of blank
            # lines" artefact (Apple/marketing mail) at render-time only. The
            # stored body is untouched — tag-matching (body_contains) and the
            # push preview read the raw value via repo/worker, not here.
            body_text=collapse_blank_lines_text(msg.body_text),
            body_html=collapse_blank_lines_html(msg.body_html),
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
        msg = await self._repo.get_for_user_ids(message_id=message_id, mail_account_ids=visible)
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
            mail_account_ids=visible,
        )
        if att is None or att.skipped_too_large:
            raise NotFoundError()
        stream: AsyncIterator[bytes] = self._storage.get_object_stream(att.s3_key)
        return att, stream

    # --- Unread counter ----------------------------------------------------

    async def count_unread_for_scope(self, scope: VisibilityScope) -> int:
        visible = await self.visible_user_ids(scope)
        return await self._repo.count_unread_for_user_ids(visible)
