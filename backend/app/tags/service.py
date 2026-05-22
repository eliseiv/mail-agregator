"""TagsService — business logic for the tags module (ADR-0017).

Source-of-truth for behaviour: ``docs/05-modules.md`` sec. 17 +
``docs/04-api-contracts.md`` section "Tags".

All public methods enforce per-user isolation (``404`` on a foreign
``tag_id``). The service does not open transactions itself — callers
(routers, the worker) wrap calls in ``async with db.begin():`` to keep
multi-step writes atomic.
"""

from __future__ import annotations

from typing import Protocol

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.exceptions import (
    CannotDeleteBuiltinTagError,
    ConflictError,
    NotFoundError,
    TagApplyTooManyError,
    ValidationError,
)
from backend.app.repositories.tags import MessageTagsRepo, TagRulesRepo, TagsRepo
from backend.app.repositories.users import UsersRepo
from backend.app.tags.builtin import BUILTIN_TAGS
from backend.app.tags.schemas import (
    PALETTE_COLORS,
    RuleDTO,
    RuleSpec,
    TagDTO,
)
from backend.app.tags.sql import APPLY_TAG_TO_EXISTING, APPLY_TAGS_TO_MESSAGE
from shared.logging import get_logger
from shared.models import Tag, TagRule


class _MessageLike(Protocol):
    """Duck-typed shape for ``apply_tags_to_message``.

    Both the ORM ``Message`` and the worker's ``_TagInputMessage`` dataclass
    satisfy this — keeps the call-site free of round-trips just to satisfy
    a strict ORM-typed signature.
    """

    @property
    def id(self) -> int: ...
    @property
    def subject(self) -> str | None: ...
    @property
    def body_text(self) -> str: ...
    @property
    def from_addr(self) -> str: ...
    @property
    def from_name(self) -> str | None: ...


log = get_logger(__name__)

# Hard limit on synchronous apply-to-existing path (ADR-0017 §7).
APPLY_TO_EXISTING_LIMIT: int = 100_000


def _to_rule_dto(rule: TagRule) -> RuleDTO:
    return RuleDTO(
        id=rule.id,
        type=rule.type,  # type: ignore[arg-type]
        pattern=rule.pattern,
        created_at=rule.created_at,
    )


def _to_tag_dto(tag: Tag, rules: list[TagRule]) -> TagDTO:
    return TagDTO(
        id=tag.id,
        name=tag.name,
        color=tag.color,
        match_mode=tag.match_mode,  # type: ignore[arg-type]
        is_builtin=tag.is_builtin,
        rules=[_to_rule_dto(r) for r in rules],
        created_at=tag.created_at,
        updated_at=tag.updated_at,
    )


class TagsService:
    def __init__(self, session: AsyncSession) -> None:
        self._db = session
        self._tags = TagsRepo(session)
        self._rules = TagRulesRepo(session)
        self._links = MessageTagsRepo(session)
        self._users = UsersRepo(session)

    async def _resolve_user_group_id(self, user_id: int) -> int | None:
        """Look up the user's ``group_id`` for visibility-scoped tag apply.

        Returns ``None`` for users without a group (super-admin and
        ungrouped users). The caller passes that NULL straight into the
        SQL where the second branch of the visibility filter is gated by
        ``:user_group_id IS NOT NULL``, so the apply naturally scopes
        down to personal accounts only.
        """
        user = await self._users.get_by_id(user_id)
        if user is None:
            # Defensive: a tag exists for a user that vanished. Treat as
            # ungrouped — the apply will scope to that user's (now empty)
            # account set, i.e. effectively a no-op.
            return None
        return user.group_id

    # --- Reads -------------------------------------------------------------

    async def list_for_user(self, user_id: int) -> list[TagDTO]:
        tags = await self._tags.list_for_user(user_id)
        rules_map = await self._rules.list_for_tags_bulk([t.id for t in tags])
        return [_to_tag_dto(t, rules_map.get(t.id, [])) for t in tags]

    async def get(self, *, user_id: int, tag_id: int) -> TagDTO:
        tag = await self._tags.get_owned(user_id, tag_id)
        if tag is None:
            raise NotFoundError()
        rules = await self._rules.list_for_tag(tag_id)
        return _to_tag_dto(tag, rules)

    # --- Writes ------------------------------------------------------------

    async def create(
        self,
        *,
        user_id: int,
        name: str,
        color: str,
        match_mode: str,
        rules: list[RuleSpec],
        apply_to_existing: bool,
    ) -> tuple[TagDTO, int]:
        """Create a custom tag plus its rules; optionally apply to existing.

        Atomic: if any step fails the caller's transaction rolls back.
        Returns the created tag DTO and the number of new ``message_tags``
        links inserted (0 unless ``apply_to_existing=True``).

        ``apply_to_existing`` is guarded against runaway scans by counting
        the user's messages first; over :data:`APPLY_TO_EXISTING_LIMIT` we
        raise :class:`TagApplyTooManyError` (422) per ADR-0017 §7.
        """
        # Optimistic message count check ahead of the heavy SQL. The
        # count must include team-visible messages because the apply
        # itself does — otherwise the guard would under-count and let an
        # apply blow past the 100k cap.
        user_group_id: int | None = None
        if apply_to_existing:
            user_group_id = await self._resolve_user_group_id(user_id)
            count = await self._links.count_messages_visible(
                user_id=user_id, user_group_id=user_group_id
            )
            if count > APPLY_TO_EXISTING_LIMIT:
                raise TagApplyTooManyError(
                    "User has too many messages for synchronous apply",
                    details={"limit": APPLY_TO_EXISTING_LIMIT, "actual": count},
                )

        try:
            tag = await self._tags.create(
                user_id=user_id,
                name=name,
                color=color,
                is_builtin=False,
                match_mode=match_mode,
            )
        except IntegrityError as exc:
            raise ConflictError("A tag with this name already exists", field="name") from exc

        if rules:
            await self._rules.add_many(
                tag_id=tag.id,
                rules=[(r.type, r.pattern) for r in rules],
            )

        applied = 0
        if apply_to_existing and rules:
            applied = await self._apply_tag_to_existing(
                user_id=user_id, user_group_id=user_group_id, tag_id=tag.id
            )

        # Reload rules so the response shape carries DB-assigned ids/timestamps.
        loaded_rules = await self._rules.list_for_tag(tag.id)
        return _to_tag_dto(tag, loaded_rules), applied

    async def update(
        self,
        *,
        user_id: int,
        tag_id: int,
        name: str | None,
        color: str | None,
        match_mode: str | None = None,
    ) -> TagDTO:
        tag = await self._tags.get_owned(user_id, tag_id)
        if tag is None:
            raise NotFoundError()
        if name is None and color is None and match_mode is None:
            # No-op; just return the current state.
            rules = await self._rules.list_for_tag(tag_id)
            return _to_tag_dto(tag, rules)
        try:
            await self._tags.update_meta(
                tag_id=tag_id, name=name, color=color, match_mode=match_mode
            )
        except IntegrityError as exc:
            raise ConflictError("A tag with this name already exists", field="name") from exc

        # Reload to get fresh ``updated_at`` (and the new name/color).
        updated = await self._tags.get_owned(user_id, tag_id)
        if updated is None:  # vanished mid-transaction
            raise NotFoundError()
        rules = await self._rules.list_for_tag(tag_id)
        return _to_tag_dto(updated, rules)

    async def delete(self, *, user_id: int, tag_id: int) -> None:
        tag = await self._tags.get_owned(user_id, tag_id)
        if tag is None:
            raise NotFoundError()
        if tag.is_builtin:
            raise CannotDeleteBuiltinTagError(
                "Builtin tags cannot be deleted; rename or edit rules instead"
            )
        await self._tags.delete(tag_id)

    async def add_rule(self, *, user_id: int, tag_id: int, type_: str, pattern: str) -> RuleDTO:
        tag = await self._tags.get_owned(user_id, tag_id)
        if tag is None:
            raise NotFoundError()
        rule = await self._rules.add(tag_id=tag_id, type_=type_, pattern=pattern)
        return _to_rule_dto(rule)

    async def delete_rule(self, *, user_id: int, tag_id: int, rule_id: int) -> None:
        tag = await self._tags.get_owned(user_id, tag_id)
        if tag is None:
            raise NotFoundError()
        rule = await self._rules.get_owned(tag_id, rule_id)
        if rule is None:
            raise NotFoundError()
        await self._rules.delete(rule_id)

    async def apply_to_existing(self, *, user_id: int, tag_id: int) -> int:
        tag = await self._tags.get_owned(user_id, tag_id)
        if tag is None:
            raise NotFoundError()
        user_group_id = await self._resolve_user_group_id(user_id)
        count = await self._links.count_messages_visible(
            user_id=user_id, user_group_id=user_group_id
        )
        if count > APPLY_TO_EXISTING_LIMIT:
            raise TagApplyTooManyError(
                "User has too many messages for synchronous apply",
                details={"limit": APPLY_TO_EXISTING_LIMIT, "actual": count},
            )
        return await self._apply_tag_to_existing(
            user_id=user_id, user_group_id=user_group_id, tag_id=tag_id
        )

    # --- Worker hooks ------------------------------------------------------

    async def apply_tags_to_message(self, *, message: _MessageLike, mail_account_id: int) -> int:
        """Run ``APPLY_TAGS_TO_MESSAGE`` for one freshly-inserted message.

        Called from ``worker.app.sync_cycle.sync_one_account`` after a
        successful ``insert_message_idempotent``. The query JOINs
        ``tags`` + ``users`` + ``mail_accounts`` so that every user who
        SEES the message — its owner plus all teammates whose
        ``users.group_id`` matches the mail account's ``group_id`` — has
        their matching tags applied. Single round trip per ADR-0017 §5.

        Returns the number of newly inserted ``message_tags`` rows; the
        caller logs this for observability.
        """
        result = await self._db.execute(
            text(APPLY_TAGS_TO_MESSAGE),
            {
                "message_id": message.id,
                "mail_account_id": mail_account_id,
                "subject": message.subject or "",
                "body": message.body_text or "",
                "sender": message.from_addr,
                # round-25: sender_contains also matches the display-name.
                # Nullable — the SQL COALESCEs NULL to '' (never matches).
                "sender_name": message.from_name,
            },
        )
        # ``Result.rowcount`` exists on the cursor backend used by asyncpg;
        # the strict ``Result[Any]`` stub doesn't expose it, hence the cast.
        rowcount = getattr(result, "rowcount", 0)
        return int(rowcount or 0)

    async def ensure_builtin_tags(self, *, user_id: int) -> int:
        """Create the builtin-tag catalogue for ``user_id`` if missing.

        Idempotent (ADR-0017 §6). Returns the number of tags created in
        this call (0 if the user already had any builtin row).

        Race-safe: a concurrent invocation that wins the ``has_any_builtin``
        race will hit the ``UNIQUE(user_id, name)`` constraint on INSERT;
        we treat that as success and short-circuit.

        round-25: each spec now carries a ``match_mode`` (``'any'``/``'all'``)
        that is persisted on the tag — see :mod:`backend.app.tags.builtin`.
        Because the catalogue was reworked, migration
        ``20260521_016_rebuild_builtin_tags`` DELETEs every existing builtin
        row so that the next login re-runs this method and recreates the new
        catalogue (the ``has_any_builtin`` short-circuit would otherwise keep
        the stale set forever).
        """
        if await self._tags.has_any_builtin(user_id):
            log.debug("builtin_tags_unchanged", user_id=user_id)
            return 0
        try:
            for spec in BUILTIN_TAGS:
                color = spec["color"]
                if color not in PALETTE_COLORS:
                    # Defence-in-depth: keeps builtin.py honest if someone
                    # edits a colour without updating the palette.
                    raise ValidationError(f"Builtin tag {spec['name']!r} colour not in palette")
                tag = await self._tags.create(
                    user_id=user_id,
                    name=spec["name"],
                    color=color,
                    is_builtin=True,
                    match_mode=spec["match_mode"],
                )
                rule_pairs = [(r["type"], r["pattern"]) for r in spec["rules"]]
                await self._rules.add_many(tag_id=tag.id, rules=rule_pairs)
        except IntegrityError:
            # Another login created the rows in parallel; treat as no-op.
            log.info("builtin_tags_race_skipped", user_id=user_id)
            return 0
        created = len(BUILTIN_TAGS)
        log.info("builtin_tags_created", user_id=user_id, count=created)
        return created

    # --- Internal helpers --------------------------------------------------

    async def _apply_tag_to_existing(
        self, *, user_id: int, user_group_id: int | None, tag_id: int
    ) -> int:
        """Bulk INSERT ``message_tags`` for every visible matching message.

        Visibility scope mirrors ``MailAccountsRepo.list_account_ids_visible``:
        personal accounts plus the user's team accounts. Pass
        ``user_group_id=None`` for users without a group — the SQL then
        narrows to personal accounts only.
        """
        result = await self._db.execute(
            text(APPLY_TAG_TO_EXISTING),
            {"tag_id": tag_id, "user_id": user_id, "user_group_id": user_group_id},
        )
        rowcount = getattr(result, "rowcount", 0)
        return int(rowcount or 0)
