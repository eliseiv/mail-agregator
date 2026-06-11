"""External PULL-API data service (ADR-0029 §1/§5).

``ExternalMessagesService.list_messages`` builds one keyset page of the
external contract. It owns ONLY data assembly — auth (API-key check) and the
rate-limit live in the router (ADR-0029 §4: router = auth + rate-limit,
service = data).

Visibility is super_admin (ALL messages of ALL teams) with the single read-path
filter being canonical-mailbox dedup (``MailAccountsRepo.list_canonical_account_ids``)
so a mailbox connected by two teams yields one copy of each message — consistent
with the super_admin inbox (round-18).
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.external.schemas import (
    ExternalMailAccountDTO,
    ExternalMessageDTO,
    ExternalMessagesResponse,
    ExternalTagDTO,
)
from backend.app.repositories.mail_accounts import MailAccountsRepo
from backend.app.repositories.messages import MessagesRepo
from backend.app.repositories.tags import MessageTagsRepo
from shared.models import Tag


class ExternalMessagesService:
    """Assemble a keyset page of external messages."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def list_messages(self, *, since_id: int, limit: int) -> ExternalMessagesResponse:
        """Return one page of messages with ``id > since_id`` (ADR-0029 §1).

        Pipeline:

        1. canonical mailbox ids (``MIN(id)`` per ``LOWER(email)``) — dedup of
           duplicate IMAP polls when two teams added the same address.
        2. keyset rows ``id > since_id ORDER BY id ASC LIMIT limit`` over those
           mailboxes.
        3. bulk-load tags for the page, dedup by ``(name, color)``.
        4. build :class:`ExternalMessageDTO` per row with **raw** bodies (no
           ``collapse_blank_lines_*`` — ADR-0029 §3/§7).

        ``next_since_id`` = last row id (or the incoming ``since_id`` on an
        empty page — cursor does not move); ``has_more`` = page was full.
        """
        canonical_ids = await MailAccountsRepo(self._db).list_canonical_account_ids()
        rows = await MessagesRepo(self._db).list_since_id(
            mail_account_ids=canonical_ids,
            since_id=since_id,
            limit=limit,
        )

        message_ids = [m.id for (m, _ma) in rows]
        tags_map = await MessageTagsRepo(self._db).list_for_messages_bulk(message_ids)

        messages: list[ExternalMessageDTO] = []
        for message, account in rows:
            # Dedup tag chips by (name, color): team-wide auto-tagging creates a
            # sibling ``tags`` row per team-member, so the raw bulk result can
            # list the same logical tag several times (mirrors messages/service).
            seen_keys: set[tuple[str, str]] = set()
            unique_tags: list[Tag] = []
            for tag in tags_map.get(message.id, []):
                key = (tag.name, tag.color)
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                unique_tags.append(tag)

            messages.append(
                ExternalMessageDTO(
                    id=message.id,
                    subject=message.subject,
                    internal_date=message.internal_date,
                    from_addr=message.from_addr,
                    from_name=message.from_name,
                    to_addrs=message.to_addrs,
                    cc_addrs=message.cc_addrs,
                    mail_account=ExternalMailAccountDTO(
                        id=account.id,
                        email=account.email,
                        display_name=account.display_name,
                    ),
                    # Raw stored bodies — NO collapse-normalisation (ADR-0029 §3/§7).
                    # ``body_present=false`` ⇒ body_text="" / body_html=None already
                    # hold in the DB (worker writes them so), surfaced verbatim.
                    body_text=message.body_text,
                    body_html=message.body_html,
                    body_present=message.body_present,
                    body_truncated=message.body_truncated,
                    tags=[ExternalTagDTO(id=t.id, name=t.name, color=t.color) for t in unique_tags],
                )
            )

        next_since_id = rows[-1][0].id if rows else since_id
        has_more = len(rows) == limit
        return ExternalMessagesResponse(
            messages=messages,
            next_since_id=next_since_id,
            has_more=has_more,
        )
