"""Repositories for ``tags``, ``tag_rules`` and ``message_tags`` (ADR-0017).

Per ``docs/05-modules.md`` sec. 17 — three classes, thin wrappers over
SQLAlchemy. Service-layer enforces ownership; repos only do the SQL.
"""

from __future__ import annotations

from collections.abc import Collection
from datetime import datetime

from sqlalchemy import and_, delete, exists, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models import MailAccount, Message, MessageTag, Tag, TagRule


class TagsRepo:
    """CRUD for the ``tags`` table."""

    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    # --- Reads -------------------------------------------------------------

    async def list_for_user(self, user_id: int) -> list[Tag]:
        stmt = select(Tag).where(Tag.user_id == user_id).order_by(Tag.is_builtin.desc(), Tag.name)
        return list((await self._s.execute(stmt)).scalars().all())

    async def get_owned(self, user_id: int, tag_id: int) -> Tag | None:
        """Return the tag iff it belongs to ``user_id`` (404-on-mismatch)."""
        stmt = select(Tag).where(Tag.id == tag_id, Tag.user_id == user_id)
        return (await self._s.execute(stmt)).scalar_one_or_none()

    async def find_by_user_name(self, user_id: int, name: str) -> Tag | None:
        stmt = select(Tag).where(Tag.user_id == user_id, Tag.name == name)
        return (await self._s.execute(stmt)).scalar_one_or_none()

    async def has_any_builtin(self, user_id: int) -> bool:
        stmt = select(exists().where(Tag.user_id == user_id, Tag.is_builtin.is_(True)))
        return bool((await self._s.execute(stmt)).scalar_one())

    # --- Global tags (ADR-0040) -------------------------------------------

    async def list_global(self) -> list[Tag]:
        """All global tags (``user_id IS NULL``) — the headless-CRM catalogue."""
        stmt = select(Tag).where(Tag.user_id.is_(None)).order_by(Tag.is_builtin.desc(), Tag.name)
        return list((await self._s.execute(stmt)).scalars().all())

    async def get_global(self, tag_id: int) -> Tag | None:
        """Return the tag iff it is global (``user_id IS NULL``) — 404-on-mismatch."""
        stmt = select(Tag).where(Tag.id == tag_id, Tag.user_id.is_(None))
        return (await self._s.execute(stmt)).scalar_one_or_none()

    async def find_global_by_name(self, name: str) -> Tag | None:
        stmt = select(Tag).where(Tag.user_id.is_(None), Tag.name == name)
        return (await self._s.execute(stmt)).scalar_one_or_none()

    # --- Writes ------------------------------------------------------------

    async def create(
        self,
        *,
        user_id: int | None,
        name: str,
        color: str,
        is_builtin: bool,
        match_mode: str = "any",
    ) -> Tag:
        # ADR-0040: ``user_id=None`` creates a GLOBAL tag (headless catalogue).
        tag = Tag(
            user_id=user_id,
            name=name,
            color=color,
            is_builtin=is_builtin,
            match_mode=match_mode,
        )
        self._s.add(tag)
        await self._s.flush()
        await self._s.refresh(tag)
        return tag

    async def update_meta(
        self,
        *,
        tag_id: int,
        name: str | None,
        color: str | None,
        match_mode: str | None = None,
    ) -> None:
        values: dict[str, object] = {}
        if name is not None:
            values["name"] = name
        if color is not None:
            values["color"] = color
        if match_mode is not None:
            values["match_mode"] = match_mode
        if not values:
            return
        # Touch updated_at explicitly so the trigger isn't relied upon when the
        # only changed column is name/color (the trigger does run, but being
        # explicit makes the intent visible in the SQL and lets us assert in
        # tests).
        values["updated_at"] = datetime.now().astimezone()
        await self._s.execute(update(Tag).where(Tag.id == tag_id).values(**values))

    async def delete(self, tag_id: int) -> None:
        await self._s.execute(delete(Tag).where(Tag.id == tag_id))


class TagRulesRepo:
    """CRUD for the ``tag_rules`` table."""

    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    # --- Reads -------------------------------------------------------------

    async def list_for_tag(self, tag_id: int) -> list[TagRule]:
        stmt = select(TagRule).where(TagRule.tag_id == tag_id).order_by(TagRule.id)
        return list((await self._s.execute(stmt)).scalars().all())

    async def list_for_tags_bulk(self, tag_ids: list[int]) -> dict[int, list[TagRule]]:
        if not tag_ids:
            return {}
        stmt = (
            select(TagRule).where(TagRule.tag_id.in_(tag_ids)).order_by(TagRule.tag_id, TagRule.id)
        )
        out: dict[int, list[TagRule]] = {tid: [] for tid in tag_ids}
        for rule in (await self._s.execute(stmt)).scalars():
            out[rule.tag_id].append(rule)
        return out

    async def get_owned(self, tag_id: int, rule_id: int) -> TagRule | None:
        stmt = select(TagRule).where(TagRule.id == rule_id, TagRule.tag_id == tag_id)
        return (await self._s.execute(stmt)).scalar_one_or_none()

    # --- Writes ------------------------------------------------------------

    async def add(self, *, tag_id: int, type_: str, pattern: str) -> TagRule:
        rule = TagRule(tag_id=tag_id, type=type_, pattern=pattern)
        self._s.add(rule)
        await self._s.flush()
        await self._s.refresh(rule)
        return rule

    async def add_many(self, *, tag_id: int, rules: list[tuple[str, str]]) -> list[TagRule]:
        if not rules:
            return []
        objs = [TagRule(tag_id=tag_id, type=t, pattern=p) for t, p in rules]
        self._s.add_all(objs)
        await self._s.flush()
        for o in objs:
            await self._s.refresh(o)
        return objs

    async def delete(self, rule_id: int) -> None:
        await self._s.execute(delete(TagRule).where(TagRule.id == rule_id))


class MessageTagsRepo:
    """CRUD for the ``message_tags`` link table."""

    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    # --- Reads -------------------------------------------------------------

    async def list_for_message(self, message_id: int) -> list[Tag]:
        """Return all tags linked to a single message, joined with ``tags``."""
        stmt = (
            select(Tag)
            .join(MessageTag, MessageTag.tag_id == Tag.id)
            .where(MessageTag.message_id == message_id)
            .order_by(Tag.is_builtin.desc(), Tag.name)
        )
        return list((await self._s.execute(stmt)).scalars().all())

    async def list_for_messages_bulk(self, message_ids: list[int]) -> dict[int, list[Tag]]:
        """Return ``{message_id: [Tag, ...]}`` for the given ids in one query.

        Used by ``MessageService.list_for_user`` to avoid N+1 when rendering
        tag-chips on the inbox.
        """
        if not message_ids:
            return {}
        stmt = (
            select(MessageTag.message_id, Tag)
            .join(Tag, Tag.id == MessageTag.tag_id)
            .where(MessageTag.message_id.in_(message_ids))
            .order_by(Tag.is_builtin.desc(), Tag.name)
        )
        out: dict[int, list[Tag]] = {mid: [] for mid in message_ids}
        for mid, tag in (await self._s.execute(stmt)).all():
            out[int(mid)].append(tag)
        return out

    async def count_messages_visible(
        self, *, user_id: int, group_ids: Collection[int], is_super_admin: bool
    ) -> int:
        """Total messages visible to ``user_id`` (ADR-0030 multi-group).

        Visibility = personal accounts (``ma.user_id = user_id``) OR
        accounts of any team the user is a member of (``ma.group_id IN
        group_ids``). Pass an empty ``group_ids`` for callers without any
        membership; the team branch is then omitted and only personal
        accounts count.

        round-26: when ``is_super_admin=True`` the count covers EVERY
        message in the system (no account join / filter), mirroring the
        super-admin reach of :data:`APPLY_TAG_TO_EXISTING`. This keeps the
        runaway guard honest — a super-admin on a >100k-message system hits
        :class:`TagApplyTooManyError` (the intended protection against a
        giant synchronous scan).

        Used by the ``apply_to_existing`` path to enforce the 100k limit
        guard documented in ADR-0017 §7. Must mirror the visibility scope
        of :data:`APPLY_TAG_TO_EXISTING` to avoid under-counting (which
        would let an apply call write more rows than the guard accounted
        for).
        """
        if is_super_admin:
            stmt = select(func.count()).select_from(Message)
            return int((await self._s.execute(stmt)).scalar_one())
        if not group_ids:
            cond = MailAccount.user_id == user_id
        else:
            cond = or_(
                MailAccount.user_id == user_id,
                and_(
                    MailAccount.group_id.is_not(None),
                    MailAccount.group_id.in_(list(group_ids)),
                ),
            )
        stmt = (
            select(func.count(Message.id))
            .join(MailAccount, MailAccount.id == Message.mail_account_id)
            .where(cond)
        )
        return int((await self._s.execute(stmt)).scalar_one())

    # --- Writes ------------------------------------------------------------

    async def link(self, *, message_id: int, tag_id: int) -> None:
        """Idempotent INSERT (``ON CONFLICT DO NOTHING``)."""
        stmt = (
            pg_insert(MessageTag)
            .values(message_id=message_id, tag_id=tag_id)
            .on_conflict_do_nothing(index_elements=["message_id", "tag_id"])
        )
        await self._s.execute(stmt)
