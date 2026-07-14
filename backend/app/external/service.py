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
    ExternalMailboxDTO,
    ExternalMailboxesResponse,
    ExternalMessageDTO,
    ExternalMessagesResponse,
    ExternalMessagesResponseDesc,
)
from backend.app.repositories.mail_accounts import MailAccountsRepo
from backend.app.repositories.messages import MessagesRepo
from shared.models import MailAccount, Message


def to_external_mailbox_dto(acc: MailAccount) -> ExternalMailboxDTO:
    """Project a ``mail_accounts`` row onto the public external mailbox DTO.

    ADR-0037 §2 + ADR-0039 §4: id/email/display_name/is_active plus the
    sync-status triplet (``last_synced_at`` / ``last_sync_error`` /
    ``consecutive_failures``). NEVER any credentials / owner / oauth / smtp / imap
    internals. Shared by the read list and the write create/update responses.

    ADR-0044 §4 (phase A1): the ``group_id`` field is dropped from the DTO — the
    aggregator has no teams (``mail_accounts.group_id`` is dropped in phase C).
    """
    return ExternalMailboxDTO(
        id=acc.id,
        email=acc.email,
        display_name=acc.display_name,
        is_active=acc.is_active,
        last_synced_at=acc.last_synced_at,
        last_sync_error=acc.last_sync_error,
        consecutive_failures=acc.consecutive_failures,
    )


class ExternalMessagesService:
    """Assemble a keyset page of external messages (forward or backward)."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def list_mailboxes(
        self,
        *,
        is_active: bool | None = None,
    ) -> ExternalMailboxesResponse:
        """Canonical mailboxes with status for the CRM (ADR-0037 §2 / ADR-0039 §4).

        Canonical-dedup (ADR-0029 §5): ``list_by_ids(list_canonical_account_ids())``
        — one ``MIN(id)`` mailbox per ``LOWER(email)``, so the set matches the
        mailboxes whose messages ``GET /api/external/messages`` returns. Exposes
        ``is_active`` (worker auto-disable) and the sync-status triplet; never any
        credentials/owner structures.

        ADR-0044 §4 (phase A1): the ``group_id`` filter is gone (no teams). Only
        ``is_active`` remains: ``None`` = all; ``True``/``False`` filters the flag.

        No mailboxes → ``{"mailboxes": []}``.
        """
        repo = MailAccountsRepo(self._db)
        canonical_ids = await repo.list_canonical_account_ids()
        accounts = await repo.list_by_ids(canonical_ids)
        if is_active is not None:
            accounts = [a for a in accounts if a.is_active == is_active]
        return ExternalMailboxesResponse(mailboxes=[to_external_mailbox_dto(a) for a in accounts])

    async def list_messages(
        self,
        *,
        order: str,
        since_id: int,
        before_id: int | None,
        limit: int,
        mail_account_ids: list[int] | None = None,
    ) -> ExternalMessagesResponse | ExternalMessagesResponseDesc:
        """Return one page in the requested direction (ADR-0029 / ADR-0036 / ADR-0039 §3).

        Validates the mode co-existence (deterministic order in
        :meth:`_validate_mode`) BEFORE any DB call, then resolves the effective
        (canonical-narrowed) mailbox set and dispatches to the forward (``asc``)
        or backward (``desc``) builder. The two builders return DISTINCT
        envelopes so each mode's cursor field is present only in its own mode
        (ADR-0036 §3).

        ADR-0039 §3: ``mail_account_id`` is a repeatable filter over the
        canonical set; an empty intersection yields an empty page (not a 404).
        The ``group_id`` filter is gone (ADR-0044 §4, phase A1 — no teams).
        """
        self._validate_mode(order=order, since_id=since_id, before_id=before_id)
        account_ids = await self._resolve_account_ids(mail_account_ids=mail_account_ids)
        if order == "asc":
            return await self._list_forward(
                mail_account_ids=account_ids, since_id=since_id, limit=limit
            )
        return await self._list_backward(
            mail_account_ids=account_ids, before_id=before_id, limit=limit
        )

    async def _resolve_account_ids(self, *, mail_account_ids: list[int] | None) -> list[int]:
        """Effective mailbox set = canonical ∩ mailboxes (ADR-0039 §3).

        - ``base`` = ``list_canonical_account_ids()`` (canonical-dedup, ADR-0029 §5).
        - ``mail_account_ids`` (if non-empty) → intersect with that id set. An
          empty / ``None`` list imposes no mailbox constraint.

        A missing / foreign / non-canonical id simply does not appear in the
        intersection (empty page, NOT 404 — ADR-0029 §3). An empty intersection
        yields an empty page; the keyset builders return ``[]`` on an empty list
        without a query, so the cursor does not move.
        """
        repo = MailAccountsRepo(self._db)
        effective: set[int] = set(await repo.list_canonical_account_ids())
        if mail_account_ids:
            effective &= set(mail_account_ids)
        return list(effective)

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

    async def _list_forward(
        self, *, mail_account_ids: list[int], since_id: int, limit: int
    ) -> ExternalMessagesResponse:
        """Forward page (ADR-0029 §1): ``id > since_id ORDER BY id ASC``.

        ``mail_account_ids`` is the already-resolved effective set (canonical set,
        optionally narrowed by the ADR-0037 filter). ``next_since_id`` = last row
        id (or the incoming ``since_id`` on an empty page — cursor does not move);
        ``has_more`` = page was full.
        """
        rows = await MessagesRepo(self._db).list_since_id(
            mail_account_ids=mail_account_ids,
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
        self, *, mail_account_ids: list[int], before_id: int | None, limit: int
    ) -> ExternalMessagesResponseDesc:
        """Backward / latest page (ADR-0036 §2): ``ORDER BY id DESC`` (newest-first).

        ``mail_account_ids`` is the already-resolved effective set (canonical set,
        optionally narrowed by the ADR-0037 filter). ``before_id is None`` ⇒
        latest N; ``before_id`` set ⇒ ``id < before_id``. ``next_before_id`` =
        last row id (= ``min(id)`` of the DESC batch), or ``None`` on an empty
        page (no older messages left); ``has_more`` = page was full. Reuses the
        same tag-dedup as forward.
        """
        rows = await MessagesRepo(self._db).list_before_id(
            mail_account_ids=mail_account_ids,
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
        """Shared DTO assembly for both directions (ADR-0036 §4).

        Builds one :class:`ExternalMessageDTO` per row with **raw** bodies (no
        ``collapse_blank_lines_*`` — ADR-0029 §3/§7). Direction only affects row
        order and the cursor field, never the per-message shape (ADR-0036 §4).

        ADR-0044 §4 (phase A1): the tag assembly
        (``MessageTagsRepo.list_for_messages_bulk``) and the ``tags`` field are
        gone — the pull no longer JOINs ``message_tags`` / ``tags``, so dropping
        them (phase D) does not break it.
        """
        return [
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
                body_text=message.body_text,
                body_html=message.body_html,
                body_present=message.body_present,
                body_truncated=message.body_truncated,
            )
            for message, account in rows
        ]
