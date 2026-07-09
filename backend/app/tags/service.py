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
from backend.app.repositories.user_groups import UserGroupsRepo
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
    def body_html(self) -> str | None: ...
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
        self._memberships = UserGroupsRepo(session)

    async def _resolve_user_group_ids(self, user_id: int) -> list[int]:
        """All teams the user belongs to (ADR-0030 — home + additional).

        Read from ``user_groups`` so the ``apply_to_existing`` count guard
        scopes to the same multi-group visibility as the apply SQL. Returns
        ``[]`` for users without any membership (super-admin / ungrouped) —
        the count then narrows to personal accounts only.
        """
        return await self._memberships.list_group_ids_for_user(user_id)

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
        is_super_admin: bool,
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
        group_ids: list[int] = []
        if apply_to_existing:
            group_ids = await self._resolve_user_group_ids(user_id)
            count = await self._links.count_messages_visible(
                user_id=user_id, group_ids=group_ids, is_super_admin=is_super_admin
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
                user_id=user_id,
                tag_id=tag.id,
                is_super_admin=is_super_admin,
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

    async def apply_to_existing(self, *, user_id: int, tag_id: int, is_super_admin: bool) -> int:
        tag = await self._tags.get_owned(user_id, tag_id)
        if tag is None:
            raise NotFoundError()
        group_ids = await self._resolve_user_group_ids(user_id)
        count = await self._links.count_messages_visible(
            user_id=user_id, group_ids=group_ids, is_super_admin=is_super_admin
        )
        if count > APPLY_TO_EXISTING_LIMIT:
            raise TagApplyTooManyError(
                "User has too many messages for synchronous apply",
                details={"limit": APPLY_TO_EXISTING_LIMIT, "actual": count},
            )
        return await self._apply_tag_to_existing(
            user_id=user_id,
            tag_id=tag_id,
            is_super_admin=is_super_admin,
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
                # round-29 (ADR-0017 §4.3): body_contains also matches the
                # tag-stripped HTML body the UI renders. Nullable — the SQL
                # COALESCEs NULL to '' (an empty string never matches).
                "body_html": message.body_html,
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

    # --- Global tags (ADR-0040 — headless-CRM catalogue, external API) -----
    #
    # All operate on the global catalogue (``tags.user_id IS NULL``). "Owned"
    # for the external path means "global" — :meth:`TagsRepo.get_global`
    # 404s on a non-global / missing id. Reuses the same repos + apply SQL as
    # the per-user path (ADR-0040 §4). Callers (the external router) wrap each
    # write in ``async with db.begin():`` for atomicity.

    async def list_global(self) -> list[TagDTO]:
        tags = await self._tags.list_global()
        rules_map = await self._rules.list_for_tags_bulk([t.id for t in tags])
        return [_to_tag_dto(t, rules_map.get(t.id, [])) for t in tags]

    async def create_global(self, *, name: str, color: str, match_mode: str) -> TagDTO:
        """Create a global tag (no rules yet, no apply). ``409`` on name clash."""
        try:
            tag = await self._tags.create(
                user_id=None,
                name=name,
                color=color,
                is_builtin=False,
                match_mode=match_mode,
            )
        except IntegrityError as exc:
            raise ConflictError("A tag with this name already exists", field="name") from exc
        return _to_tag_dto(tag, [])

    async def update_global(
        self,
        *,
        tag_id: int,
        name: str | None,
        color: str | None,
        match_mode: str | None = None,
    ) -> TagDTO:
        tag = await self._tags.get_global(tag_id)
        if tag is None:
            raise NotFoundError()
        if name is None and color is None and match_mode is None:
            rules = await self._rules.list_for_tag(tag_id)
            return _to_tag_dto(tag, rules)
        try:
            await self._tags.update_meta(
                tag_id=tag_id, name=name, color=color, match_mode=match_mode
            )
        except IntegrityError as exc:
            raise ConflictError("A tag with this name already exists", field="name") from exc
        updated = await self._tags.get_global(tag_id)
        if updated is None:  # vanished mid-transaction
            raise NotFoundError()
        rules = await self._rules.list_for_tag(tag_id)
        return _to_tag_dto(updated, rules)

    async def delete_global(self, *, tag_id: int) -> None:
        tag = await self._tags.get_global(tag_id)
        if tag is None:
            raise NotFoundError()
        if tag.is_builtin:
            # ADR-0040 §4 / 04-api-contracts §4f-tags: a builtin tag cannot be
            # deleted — surfaced as ``409 conflict`` on the external contract
            # (rename / rule edits stay allowed).
            raise ConflictError("Builtin tags cannot be deleted; rename or edit rules instead")
        await self._tags.delete(tag_id)

    async def add_rule_global(self, *, tag_id: int, type_: str, pattern: str) -> RuleDTO:
        tag = await self._tags.get_global(tag_id)
        if tag is None:
            raise NotFoundError()
        rule = await self._rules.add(tag_id=tag_id, type_=type_, pattern=pattern)
        return _to_rule_dto(rule)

    async def delete_rule_global(self, *, tag_id: int, rule_id: int) -> None:
        tag = await self._tags.get_global(tag_id)
        if tag is None:
            raise NotFoundError()
        rule = await self._rules.get_owned(tag_id, rule_id)
        if rule is None:
            raise NotFoundError()
        await self._rules.delete(rule_id)

    async def apply_to_existing_global(self, *, tag_id: int) -> int:
        """Apply a global tag's rules to EVERY existing message (ADR-0040 §4).

        Global reach is expressed via the super-admin short-circuit already in
        :data:`APPLY_TAG_TO_EXISTING` (``is_super_admin=True`` forces the
        visibility filter to TRUE for all rows). ``user_id`` is irrelevant on
        this path — a sentinel ``0`` is passed only to satisfy the bind. The
        ``APPLY_TO_EXISTING_LIMIT`` guard counts the whole corpus (super-admin
        count) exactly as the apply reaches it.
        """
        tag = await self._tags.get_global(tag_id)
        if tag is None:
            raise NotFoundError()
        count = await self._links.count_messages_visible(
            user_id=0, group_ids=[], is_super_admin=True
        )
        if count > APPLY_TO_EXISTING_LIMIT:
            raise TagApplyTooManyError(
                "Too many messages for synchronous apply",
                details={"limit": APPLY_TO_EXISTING_LIMIT, "actual": count},
            )
        return await self._apply_tag_to_existing(user_id=0, tag_id=tag_id, is_super_admin=True)

    # --- Internal helpers --------------------------------------------------

    async def _apply_tag_to_existing(
        self, *, user_id: int, tag_id: int, is_super_admin: bool
    ) -> int:
        """Bulk INSERT ``message_tags`` for every visible matching message.

        Visibility scope mirrors ``MailAccountsRepo.list_account_ids_visible``
        (ADR-0030 multi-group): personal accounts plus accounts of any team
        the owner is a member of. The SQL keys the team branch on ``:user_id``
        via ``user_groups`` (EXISTS), so no per-group bind is needed and a
        user without any membership narrows to personal accounts only.

        round-26: when ``is_super_admin=True`` the apply reaches EVERY
        message in the system (the SQL visibility filter short-circuits to
        TRUE), matching the super-admin read scope.
        """
        result = await self._db.execute(
            text(APPLY_TAG_TO_EXISTING),
            {
                "tag_id": tag_id,
                "user_id": user_id,
                "is_super_admin": is_super_admin,
            },
        )
        rowcount = getattr(result, "rowcount", 0)
        return int(rowcount or 0)


async def seed_builtin_tags(session: AsyncSession) -> int:
    """Idempotently seed the GLOBAL builtin-tag catalogue (ADR-0040 §3).

    Called from the API lifespan (``backend.app.main.create_app``) by the
    pattern of ``seed_super_admin`` — replaces the previous per-login lazy
    creation. Each spec is created only when a global tag of that name does not
    already exist (``uq_tags_global_name``), so re-running the boot is a no-op.

    Race-safety: each tag+rules insert runs in its own SAVEPOINT
    (``begin_nested``); if a concurrent process wins the name race the partial
    unique index raises :class:`IntegrityError` which rolls back only that
    savepoint (leaving the outer transaction and already-seeded tags intact).
    Colours are validated against ``PALETTE_COLORS`` (defence-in-depth).

    Returns the number of tags created in this call (0 if all already present).
    """
    tags = TagsRepo(session)
    rules = TagRulesRepo(session)
    created = 0
    for spec in BUILTIN_TAGS:
        color = spec["color"]
        if color not in PALETTE_COLORS:
            # Defence-in-depth: keeps builtin.py honest if someone edits a
            # colour without updating the palette.
            raise ValidationError(f"Builtin tag {spec['name']!r} colour not in palette")
        if await tags.find_global_by_name(spec["name"]) is not None:
            continue
        try:
            async with session.begin_nested():
                tag = await tags.create(
                    user_id=None,
                    name=spec["name"],
                    color=color,
                    is_builtin=True,
                    match_mode=spec["match_mode"],
                )
                rule_pairs = [(r["type"], r["pattern"]) for r in spec["rules"]]
                await rules.add_many(tag_id=tag.id, rules=rule_pairs)
            created += 1
        except IntegrityError:
            # Concurrent seed created this global name first — treat as present.
            log.info("builtin_tags_seed_race_skipped", name=spec["name"])
            continue
    if created:
        log.info("builtin_tags_seeded", count=created)
    else:
        log.debug("builtin_tags_seed_unchanged")
    return created
