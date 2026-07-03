"""External PULL-API data service (ADR-0029 §1/§5 + ADR-0036 backward mode).

``ExternalMessagesService.list_messages`` builds one keyset page of the
external contract in either direction:

- ``order=asc`` — forward keyset (ADR-0029, oldest→newest, cursor
  ``next_since_id``). Byte-for-byte unchanged.
- ``order=desc`` — backward / latest (ADR-0036, newest-first, cursor
  ``next_before_id``); ``before_id`` absent ⇒ latest N, present ⇒ older page.

The service owns ONLY data assembly + the mode-selection validation (ADR-0036
§5); auth (API-key check) and the rate-limit live in the router (ADR-0029 §4:
router = auth + rate-limit, service = data). Validation runs AFTER auth because
the router calls the service only once auth has passed (ADR-0036 §6).

Visibility is super_admin (ALL messages of ALL teams); the single read-path
filter is canonical-mailbox dedup (``MailAccountsRepo.list_canonical_account_ids``)
so a mailbox connected by two teams yields one copy of each message — applied
identically in both directions (ADR-0036 §2).
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.exceptions import ValidationError
from backend.app.external.schemas import (
    ExternalMailAccountDTO,
    ExternalMessageDTO,
    ExternalMessagesResponse,
    ExternalMessagesResponseDesc,
    ExternalTagDTO,
)
from backend.app.repositories.mail_accounts import MailAccountsRepo
from backend.app.repositories.messages import MessagesRepo
from backend.app.repositories.tags import MessageTagsRepo
from shared.models import MailAccount, Message, Tag


class ExternalMessagesService:
    """Assemble a keyset page of external messages (forward or backward)."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def list_messages(
        self,
        *,
        order: str,
        since_id: int,
        before_id: int | None,
        limit: int,
    ) -> ExternalMessagesResponse | ExternalMessagesResponseDesc:
        """Return one page in the requested direction (ADR-0029 / ADR-0036).

        Validates the mode co-existence (deterministic order below) then
        dispatches to the forward (``asc``) or backward (``desc``) builder. The
        two builders return DISTINCT envelopes so each mode's cursor field is
        present only in its own mode (ADR-0036 §3).
        """
        self._validate_mode(order=order, since_id=since_id, before_id=before_id)
        if order == "asc":
            return await self._list_forward(since_id=since_id, limit=limit)
        return await self._list_backward(before_id=before_id, limit=limit)

    @staticmethod
    def _validate_mode(*, order: str, since_id: int, before_id: int | None) -> None:
        """Mode co-existence validation with a DETERMINISTIC check order.

        ADR-0036 §5 + architect-reviewer minor: when several constraints are
        violated at once the returned ``field`` must be predictable, so the
        checks run STRICTLY in this sequence (each raises ``400
        validation_error`` with the stated ``field``):

        1. enum ``order`` ∈ {``asc``, ``desc``}                → ``field=order``
        2. per-cursor mode mismatch:
             - ``before_id`` present while ``order=asc``       → ``field=before_id``
             - ``since_id`` set (``!= 0`` default) while ``order=desc``
                                                               → ``field=since_id``
        3. both cursors present together (``since_id != 0`` AND ``before_id``)
                                                               → ``field=cursor``
        4. ``before_id < 1``                                   → ``field=before_id``

        ``since_id < 0`` and ``limit`` bounds are enforced upstream by FastAPI
        ``Query`` (ADR-0029, unchanged) and never reach this function.

        Note on step 3: step 2 already rejects ``asc``+``before_id`` and
        ``desc``+``since_id``, so the only way to reach step 3 with both cursors
        set is already covered above; the check is kept explicit to match the
        ADR-0036 §5 table (``field=cursor``) and stay robust to future reorders.
        """
        # (1) enum first — an invalid direction wins over any cursor error.
        if order not in ("asc", "desc"):
            raise ValidationError("order must be 'asc' or 'desc'", field="order")
        # (2) per-cursor mode mismatch (before_id checked before since_id).
        if order == "asc" and before_id is not None:
            raise ValidationError("before_id допустим только при order=desc", field="before_id")
        if order == "desc" and since_id != 0:
            raise ValidationError("since_id допустим только при order=asc", field="since_id")
        # (3) both cursors together (defensive — see docstring note).
        if since_id != 0 and before_id is not None:
            raise ValidationError("since_id и before_id взаимоисключающи", field="cursor")
        # (4) before_id lower bound LAST, so a mode/order error surfaces first.
        if before_id is not None and before_id < 1:
            raise ValidationError("before_id must be >= 1", field="before_id")

    async def _list_forward(self, *, since_id: int, limit: int) -> ExternalMessagesResponse:
        """Forward page (ADR-0029 §1): ``id > since_id ORDER BY id ASC``.

        ``next_since_id`` = last row id (or the incoming ``since_id`` on an
        empty page — cursor does not move); ``has_more`` = page was full.
        """
        canonical_ids = await MailAccountsRepo(self._db).list_canonical_account_ids()
        rows = await MessagesRepo(self._db).list_since_id(
            mail_account_ids=canonical_ids,
            since_id=since_id,
            limit=limit,
        )
        messages = await self._build_dtos(rows)
        next_since_id = rows[-1][0].id if rows else since_id
        return ExternalMessagesResponse(
            messages=messages,
            next_since_id=next_since_id,
            has_more=len(rows) == limit,
        )

    async def _list_backward(
        self, *, before_id: int | None, limit: int
    ) -> ExternalMessagesResponseDesc:
        """Backward / latest page (ADR-0036 §2): ``ORDER BY id DESC`` (newest-first).

        ``before_id is None`` ⇒ latest N; ``before_id`` set ⇒ ``id < before_id``.
        ``next_before_id`` = last row id (= ``min(id)`` of the DESC batch), or
        ``None`` on an empty page (no older messages left); ``has_more`` = page
        was full. Reuses the same canonical-scope + tag-dedup as forward.
        """
        canonical_ids = await MailAccountsRepo(self._db).list_canonical_account_ids()
        rows = await MessagesRepo(self._db).list_before_id(
            mail_account_ids=canonical_ids,
            before_id=before_id,
            limit=limit,
        )
        messages = await self._build_dtos(rows)
        next_before_id = rows[-1][0].id if rows else None
        return ExternalMessagesResponseDesc(
            messages=messages,
            next_before_id=next_before_id,
            has_more=len(rows) == limit,
        )

    async def _build_dtos(
        self, rows: list[tuple[Message, MailAccount]]
    ) -> list[ExternalMessageDTO]:
        """Shared DTO assembly for both directions (ADR-0036 §4: tags in both).

        Bulk-loads tags for the page and dedups them by ``(name, color)`` (the
        same team-wide auto-tagging sibling-collapse as the UI inbox), then
        builds one :class:`ExternalMessageDTO` per row with **raw** bodies (no
        ``collapse_blank_lines_*`` — ADR-0029 §3/§7). Direction only affects row
        order and the cursor field, never the per-message shape (ADR-0036 §4).
        """
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
        return messages
