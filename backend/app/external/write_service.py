"""External WRITE-API orchestration — mailboxes (ADR-0039 §2).

Backs the headless-CRM write section (``docs/04-api-contracts.md`` §4f) and
REUSES the canonical :class:`backend.app.accounts.service.MailAccountService`
(create/test/update/delete/force-sync, incl. the IMAP/SMTP probe and the SSRF
guard ``assert_public_host``) instead of re-implementing the logic.

ADR-0044 §4 (phase A1): the global-tag section (``ExternalTagsService``,
``TagsService``) went away with tags; the ``group_id`` pass-through went away
with teams.

The external path has no interactive ``VisibilityScope``: mailboxes belong to
the ``crm-service`` technical super_admin (ADR-0039 §Q-0039-1), for which a
SYNTHETIC super_admin scope is built so the reused owner validation resolves
deterministically. A duplicate ``(crm-service, email)`` → ``409 conflict
field=email``.

The router owns the auth-flow (rate-limit → key → gate → write-gate) and wraps
each write in ``async with db.begin():``.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.accounts.schemas import (
    MailAccountCreateRequest,
    MailAccountTestRequest,
    MailAccountUpdateRequest,
)
from backend.app.accounts.service import MailAccountService
from backend.app.auth.service import CRM_SERVICE_USERNAME
from backend.app.deps import VisibilityScope
from backend.app.exceptions import DependencyUnavailableError, NotFoundError
from backend.app.external.schemas import (
    ExternalMailboxCreateRequest,
    ExternalMailboxDTO,
    ExternalMailboxTestRequest,
    ExternalMailboxTestResponse,
    ExternalMailboxUpdateRequest,
)
from backend.app.external.service import to_external_mailbox_dto
from backend.app.repositories.mail_accounts import MailAccountsRepo
from backend.app.repositories.users import UsersRepo


class ExternalMailboxService:
    """Mailbox CRUD for the external write API (ADR-0039 §2)."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db
        self._accounts = MailAccountService(db)
        self._repo = MailAccountsRepo(db)

    async def flush_crm_status_events(self) -> None:
        """Fire the deferred mailbox-status hooks (ADR-0046 §2 — H5/H6).

        Delegates to the reused :class:`MailAccountService`. The router MUST call
        this AFTER leaving its ``async with db.begin():`` block: the dispatcher
        reads the live DB snapshot, and for a ``is_active=false`` PATCH there is
        no second chance — a deactivated mailbox leaves ``list_active()`` and
        never emits another status event.
        """
        await self._accounts.flush_crm_status_events()

    async def _crm_scope(self) -> VisibilityScope:
        """Synthetic super_admin scope for the ``crm-service`` owner (ADR-0039)."""
        user = await UsersRepo(self._db).get_by_username(CRM_SERVICE_USERNAME)
        if user is None:
            # Seeded at startup (``seed_crm_service_user``); this is an
            # impossible post-boot state. Surface a clean 503 rather than a
            # confusing owner-resolution error.
            raise DependencyUnavailableError("crm-service technical user is not provisioned")
        return VisibilityScope(
            user_id=user.id,
            role="super_admin",
            group_id=None,
            group_ids=frozenset(),
        )

    async def test(self, payload: ExternalMailboxTestRequest) -> ExternalMailboxTestResponse:
        """IMAP/SMTP connectivity probe without persistence (ad-hoc test mode)."""
        req = MailAccountTestRequest(
            email=payload.email,
            password=payload.password,
            imap_host=payload.imap_host,
            imap_port=payload.imap_port,
            imap_ssl=payload.imap_ssl,
            smtp_host=payload.smtp_host,
            smtp_port=payload.smtp_port,
            smtp_ssl=payload.smtp_ssl,
            smtp_starttls=payload.smtp_starttls,
            smtp_username=payload.smtp_username,
            smtp_password=payload.smtp_password,
        )
        result = await self._accounts.test(req)
        return ExternalMailboxTestResponse(imap_ok=result.imap_ok, smtp_ok=result.smtp_ok)

    async def create(self, payload: ExternalMailboxCreateRequest) -> ExternalMailboxDTO:
        scope = await self._crm_scope()
        create_req = MailAccountCreateRequest(
            email=payload.email,
            password=payload.password,
            imap_host=payload.imap_host,
            imap_port=payload.imap_port,
            imap_ssl=payload.imap_ssl,
            smtp_host=payload.smtp_host,
            smtp_port=payload.smtp_port,
            smtp_ssl=payload.smtp_ssl,
            smtp_starttls=payload.smtp_starttls,
            smtp_username=payload.smtp_username,
            smtp_password=payload.smtp_password,
            display_name=payload.display_name,
            target_user_id=scope.user_id,  # owner = crm-service
        )
        dto = await self._accounts.create(scope=scope, payload=create_req)
        acc = await self._repo.get_by_id(dto.id)
        if acc is None:  # created moments ago in this tx — defensive
            raise NotFoundError()
        return to_external_mailbox_dto(acc)

    async def update(
        self, account_id: int, payload: ExternalMailboxUpdateRequest
    ) -> ExternalMailboxDTO:
        scope = await self._crm_scope()

        if payload.has_account_fields:
            # Credential / host / display_name change → reuse the full update
            # path (re-test on creds, SSRF guard). ADR-0044 §4 (phase A1): the
            # ``group_id`` pass-through went away with teams.
            upd_kwargs: dict[str, object] = {
                "email": payload.email,
                "password": payload.password,
                "display_name": payload.display_name,
                "imap_host": payload.imap_host,
                "imap_port": payload.imap_port,
                "imap_ssl": payload.imap_ssl,
                "smtp_host": payload.smtp_host,
                "smtp_port": payload.smtp_port,
                "smtp_ssl": payload.smtp_ssl,
                "smtp_starttls": payload.smtp_starttls,
                "smtp_username": payload.smtp_username,
                "smtp_password": payload.smtp_password,
            }
            upd = MailAccountUpdateRequest.model_validate(upd_kwargs)
            await self._accounts.update(scope=scope, account_id=account_id, payload=upd)

        if payload.set_is_active:
            # Activate / deactivate — no reusable field on the internal update
            # request, so it goes through the dedicated ``set_active`` service
            # method (ADR-0033 re-enable semantics on activate).
            await self._accounts.set_active(
                scope=scope, account_id=account_id, is_active=bool(payload.is_active)
            )
        elif not payload.has_account_fields:
            # Empty PATCH: still confirm the mailbox exists (404 otherwise).
            visible = await self._accounts.visible_user_ids(scope)
            if await self._repo.get_for_user_ids(visible, account_id) is None:
                raise NotFoundError()

        acc = await self._repo.get_by_id(account_id)
        if acc is None:
            raise NotFoundError()
        return to_external_mailbox_dto(acc)

    async def delete(self, account_id: int) -> None:
        scope = await self._crm_scope()
        await self._accounts.delete(scope=scope, account_id=account_id)

    async def sync(self, account_id: int) -> None:
        scope = await self._crm_scope()
        await self._accounts.force_sync(scope=scope, account_id=account_id)
