"""MailAccountService — CRUD + test-login + force-sync marker.

Reused by the external write API (``backend/app/external/write_service.py``)
— the ONLY caller left after the decommission (ADR-0044 §4, phase A3). The
scope is always the synthetic ``crm-service`` super_admin; team-based
visibility, the ``admin_audit`` writer and the MinIO attachment cascade went
away with ``groups`` / ``admin_audit`` / MinIO (ADR-0043 §4).
"""

from __future__ import annotations

import asyncio
from typing import Literal

from sqlalchemy import event as sa_event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session as SyncSession
from sqlalchemy.orm import SessionTransaction

from backend.app.accounts.schemas import (
    MailAccountCreateRequest,
    MailAccountDTO,
    MailAccountTestRequest,
    MailAccountUpdateRequest,
    OwnerBriefDTO,
    TestResult,
)
from backend.app.accounts.testers import (
    imap_test_login,
    imap_test_oauth,
    smtp_test_login,
    smtp_test_oauth,
)
from backend.app.crm_push.service import enqueue_crm_status_best_effort
from backend.app.deps import VisibilityScope
from backend.app.exceptions import (
    ConflictError,
    ForbiddenError,
    IMAPLoginFailedError,
    NotFoundError,
    OAuthReconsentRequiredError,
    SMTPLoginFailedError,
    ValidationError,
)
from backend.app.oauth.service import OAuthRefreshInvalidError, OutlookTokenService
from backend.app.repositories.mail_accounts import MailAccountsRepo
from backend.app.repositories.users import UsersRepo
from shared.config import get_settings
from shared.credentials import normalize_optional_login, normalize_optional_secret
from shared.crypto import decrypt_mail_password, encrypt_mail_password
from shared.logging import get_logger
from shared.models import MailAccount, User
from shared.redis_client import get_redis
from shared.session_guards import SessionGuard, register_session_guard

log = get_logger(__name__)

#: Structured-log event of the TD-054 detector: a caller committed a
#: status-writing change and never called ``flush_crm_status_events()`` — the
#: mailbox-status event is lost (for a deactivation: forever, ADR-0046 §2.1.1).
CRM_STATUS_PENDING_DROPPED_EVENT = "crm_status_pending_dropped"


def _require(value: str | None, field: str) -> str:
    """Narrow an optional credential field to ``str`` for the ad-hoc test path.

    The :class:`MailAccountTestRequest` validator already guarantees these are
    present when ``account_id`` is unset; this keeps mypy honest at the call
    site and degrades to a clear 400 if a caller bypasses the schema.
    """
    if not value:
        raise ValidationError(f"{field} is required", field=field)
    return value


#: Stage of a connection-test probe, used to attribute a hard-deadline
#: expiry to a domain error (ADR-0047 §3).
ProbeStageName = Literal["imap", "smtp", "oauth_token"]


class _ProbeStage:
    """Mutable marker of the stage a probe is currently in (ADR-0047 §3).

    The probe body updates it as it advances; the deadline handler reads it to
    pick the domain error DETERMINISTICALLY instead of guessing.
    """

    __slots__ = ("value",)

    def __init__(self, initial: ProbeStageName) -> None:
        self.value: ProbeStageName = initial


def _deadline_error(stage: ProbeStageName) -> IMAPLoginFailedError | SMTPLoginFailedError:
    """Translate a hard-deadline expiry into an EXISTING domain error (ADR-0047 §3).

    Only ``imap_login_failed`` / ``smtp_login_failed`` (both 422) are allowed: a
    NEW error code would land in the CRM's ``422 unprocessable`` fallback and the
    reason of the failure — the whole point of the deadline — would be lost again
    (ADR-0047 §3, CRM ``ADR-053`` §2). ``oauth_token`` (the refresh exchange with
    Microsoft ran out) precedes IMAP and means "no connection to the mailbox", so
    it is attributed to ``imap_login_failed``. ``details`` is a free-form field
    (the CRM reads only ``error.code``), so ``stage`` does not widen the contract.
    """
    if stage == "smtp":
        return SMTPLoginFailedError(
            "SMTP connection test timed out",
            details={"detail": "timeout", "stage": stage},
        )
    return IMAPLoginFailedError(
        "IMAP connection test timed out",
        details={"detail": "timeout", "stage": stage},
    )


def _to_dto(acc: MailAccount, owner: User) -> MailAccountDTO:
    return MailAccountDTO(
        id=acc.id,
        user_id=acc.user_id,
        owner=OwnerBriefDTO(
            id=owner.id,
            username=owner.username,
            display_name=owner.display_name,
        ),
        email=acc.email,
        display_name=acc.display_name,
        auth_type=acc.auth_type,
        oauth_needs_consent=acc.oauth_needs_consent,
        imap_host=acc.imap_host,
        imap_port=acc.imap_port,
        imap_ssl=acc.imap_ssl,
        smtp_host=acc.smtp_host,
        smtp_port=acc.smtp_port,
        smtp_ssl=acc.smtp_ssl,
        smtp_starttls=acc.smtp_starttls,
        # Garbage-in-DB guard: a stored ``'None'`` / blank means "no SMTP login"
        # (shared/credentials.py) — never echo it back as if it were a login.
        smtp_username=normalize_optional_login(acc.smtp_username),
        is_active=acc.is_active,
        last_synced_at=acc.last_synced_at,
        last_sync_error=acc.last_sync_error,
        consecutive_failures=acc.consecutive_failures,
        created_at=acc.created_at,
    )


class MailAccountService:
    def __init__(self, session: AsyncSession) -> None:
        self._db = session
        self._repo = MailAccountsRepo(session)
        self._users = UsersRepo(session)
        # ADR-0046 §2 — mailbox-status hooks (H5 creds re-enable / H6 set_active)
        # are DEFERRED here and fired by the router AFTER the request
        # transaction commits (see :meth:`flush_crm_status_events`). This
        # service runs INSIDE the caller's ``async with db.begin():`` block, so
        # enqueuing here would let the dispatcher (which loads the live DB
        # snapshot) ship the pre-commit state and stick.
        self._pending_status_account_ids: list[int] = []
        # Number of LEADING entries of ``_pending_status_account_ids`` whose
        # transaction already COMMITted (see :meth:`_mark_pending_status_committed`).
        # Those ids describe status ALREADY WRITTEN to the DB: the flush stays
        # mandatory for them, so a LATER rollback (a failed SELECT in the next
        # implicit txn, an explicit ``db.rollback()`` in an error handler) must
        # NOT drain them — otherwise the detector would go silent exactly where
        # the event is genuinely lost (TD-054 false negative).
        self._status_committed_count = 0
        # TD-054 — runtime DETECTOR for the failure mode deferred-flush creates:
        # a caller that invokes a status-writing method (``update`` on
        # ``creds_changed`` / ``set_active`` — the closed list of ADR-0046
        # §2.1.1) and never calls :meth:`flush_crm_status_events` used to lose
        # the event SILENTLY. The guard fires at session teardown
        # (``shared.db.get_session`` / ``make_session`` — the only two session
        # sources, so ANY caller is covered): warning in prod, hard fail under
        # pytest. It never sends the event itself (auto-flush is rejected in
        # ADR-0046 §Alternatives: teardown cannot know whether the txn
        # committed).
        #
        # The probe closes over the pending LIST, never over ``self``: the guard
        # is stored as the VALUE of a ``WeakKeyDictionary`` keyed by the session
        # (``shared.session_guards``), and ``self._db`` IS that session — a probe
        # capturing ``self`` would make the value hold a strong ref to its own
        # key and defeat the weakness (any session that skips the ``shared.db``
        # teardown would then be pinned for the life of the process). The list
        # object is never rebound (drained in place), so it stays the single
        # source of truth for both the service and the probe.
        pending_status_account_ids = self._pending_status_account_ids
        register_session_guard(
            session,
            SessionGuard(
                event=CRM_STATUS_PENDING_DROPPED_EVENT,
                field="mail_account_ids",
                owner="MailAccountService",
                probe=lambda: tuple(pending_status_account_ids),
            ),
        )
        # A ROLLED BACK transaction wrote no status, so there is nothing to
        # mirror and nothing was "dropped" — drain the queue so the detector
        # never cries wolf on the legitimate error path (e.g. a 409 raised
        # inside the router's ``db.begin()``). Savepoint rollbacks
        # (``begin_nested``) do not end the unit of work → ignored. Only the
        # ids appended in the ROLLED BACK transaction are dropped — ids already
        # COMMITted survive (see ``_status_committed_count``).
        sa_event.listen(
            session.sync_session,
            "after_commit",
            self._mark_pending_status_committed,
        )
        sa_event.listen(
            session.sync_session,
            "after_soft_rollback",
            self._discard_pending_status_on_rollback,
        )

    # --- CRM status hooks (ADR-0046 §2) ------------------------------------

    def _mark_pending_status_committed(self, session: SyncSession) -> None:
        """Freeze the ids whose status write reached the DB (TD-054).

        SQLAlchemy dispatches ``after_commit`` for a SAVEPOINT release too
        (``SessionTransaction.commit``: ``if self._parent is None or self.nested``),
        and a savepoint release does not end the unit of work — the outer
        transaction may still roll back. ``get_nested_transaction()`` is the
        discriminator: it is non-``None`` exactly while the committing
        transaction is the nested one, so only a REAL (outermost) commit marks
        the queue as durable.
        """
        if session.get_nested_transaction() is not None:
            return
        self._status_committed_count = len(self._pending_status_account_ids)

    def _discard_pending_status_on_rollback(
        self, session: SyncSession, previous_transaction: SessionTransaction
    ) -> None:
        """Drop deferred status events whose transaction rolled back (TD-054)."""
        if previous_transaction.nested:
            return
        del self._pending_status_account_ids[self._status_committed_count :]

    async def flush_crm_status_events(self) -> None:
        """Fire the mailbox-status hooks collected during this unit of work.

        MUST be called by the caller STRICTLY AFTER the COMMIT of the request
        transaction (ADR-0046 §2) — never inside ``async with db.begin():``.
        Best-effort and idempotent: the queue is drained, so a repeated call is
        a no-op, and a rolled-back transaction simply never reaches the flush.
        """
        pending = list(self._pending_status_account_ids)
        # Drained IN PLACE (never rebound): the TD-054 guard holds this very
        # list object, so a rebind would leave the detector probing a stale
        # queue and fire on an already-flushed request.
        self._pending_status_account_ids.clear()
        self._status_committed_count = 0
        for account_id in pending:
            await enqueue_crm_status_best_effort(account_id)

    # --- Visibility helpers ------------------------------------------------

    async def visible_user_ids(self, scope: VisibilityScope) -> list[int] | None:
        """Set of ``mail_accounts.id`` visible to the caller.

        ADR-0044 §4 (phase A3): team-based visibility went away with
        ``mail_accounts.group_id``. The only caller left is the synthetic
        ``crm-service`` super_admin scope (``external/write_service.py``),
        which sees every mailbox.

        ``None`` = "no scope filter" (super-admin path). A non-super_admin scope
        cannot even be built in the headless connector (there are no sessions),
        so it is rejected with an explicit 403 instead of silently returning an
        empty list.
        """
        if not scope.is_super_admin:
            raise ForbiddenError("Only the crm-service scope may manage mailboxes")
        return None

    async def _visible_user_ids(self, scope: VisibilityScope) -> list[int] | None:
        return await self.visible_user_ids(scope)

    # --- Test login --------------------------------------------------------

    async def test(
        self,
        payload: MailAccountTestRequest,
        *,
        scope: VisibilityScope | None = None,
    ) -> TestResult:
        """Test connectivity.

        Two modes (ADR-0025 §4c):

        - ``payload.account_id`` set → resolve the stored account (within the
          caller's visibility ``scope``) and re-test it with its persisted
          secrets. ``oauth_outlook`` accounts go the XOAUTH2 path
          (refresh→access→connect); password accounts re-probe with the
          stored password.
        - otherwise → ad-hoc credential test using the submitted fields
          (account-creation flow).
        """
        if payload.account_id is not None:
            if scope is None:
                # Defensive: the existing-account path is only reachable from
                # the router, which always supplies a scope.
                raise ValidationError("account_id requires an authenticated scope")
            return await self._test_existing_account(scope, payload.account_id)
        return await self._test_credentials(
            email=_require(payload.email, "email"),
            password=_require(payload.password, "password"),
            imap_host=_require(payload.imap_host, "imap_host"),
            imap_port=payload.imap_port,
            imap_ssl=payload.imap_ssl,
            smtp_host=_require(payload.smtp_host, "smtp_host"),
            smtp_port=payload.smtp_port,
            smtp_ssl=payload.smtp_ssl,
            smtp_starttls=payload.smtp_starttls,
            smtp_username=payload.smtp_username,
            smtp_password=payload.smtp_password,
        )

    async def _test_credentials(
        self,
        *,
        email: str,
        password: str,
        imap_host: str,
        imap_port: int,
        imap_ssl: bool,
        smtp_host: str,
        smtp_port: int,
        smtp_ssl: bool,
        smtp_starttls: bool,
        smtp_username: str | None,
        smtp_password: str | None,
    ) -> TestResult:
        """Password probe (P1) under the hard-deadline (ADR-0047 §1).

        The ``wait_for`` lives HERE, inside the probe method, and NOT at the call
        site: ``_test_credentials`` has TWO callers — :meth:`test` (ad-hoc creds)
        and :meth:`_test_existing_account` (re-probe of a stored mailbox, also
        reachable from ``POST /test`` with ``account_id``) — so a call-site
        wrapper would leave one of them unbounded. Bound to the method, every
        future caller (``create`` / ``update`` already among them) inherits the
        deadline for free. A SECOND deadline around this method (in ``test`` /
        ``create`` / ``update`` / a router) is forbidden — it would mask this one
        (ADR-0047 §1.1).
        """
        stage = _ProbeStage("imap")
        try:
            return await asyncio.wait_for(
                self._test_credentials_inner(
                    stage=stage,
                    email=email,
                    password=password,
                    imap_host=imap_host,
                    imap_port=imap_port,
                    imap_ssl=imap_ssl,
                    smtp_host=smtp_host,
                    smtp_port=smtp_port,
                    smtp_ssl=smtp_ssl,
                    smtp_starttls=smtp_starttls,
                    smtp_username=smtp_username,
                    smtp_password=smtp_password,
                ),
                timeout=get_settings().MAILBOX_TEST_DEADLINE_SECONDS,
            )
        except TimeoutError as exc:
            raise _deadline_error(stage.value) from exc

    async def _test_credentials_inner(
        self,
        *,
        stage: _ProbeStage,
        email: str,
        password: str,
        imap_host: str,
        imap_port: int,
        imap_ssl: bool,
        smtp_host: str,
        smtp_port: int,
        smtp_ssl: bool,
        smtp_starttls: bool,
        smtp_username: str | None,
        smtp_password: str | None,
    ) -> TestResult:
        """Body of P1: host-assert + IMAP login, then host-assert + SMTP login.

        Both host-asserts resolve OFF the event loop (ADR-0047 §4) — otherwise
        the deadline above could not fire while ``getaddrinfo`` blocks the loop.
        """
        stage.value = "imap"
        await imap_test_login(
            host=imap_host,
            port=imap_port,
            ssl_on=imap_ssl,
            username=email,
            password=password,
        )
        stage.value = "smtp"
        # The SMTP probe must resolve the login/secret EXACTLY like the send path
        # (``send/service.smtp_send_message``): absence sentinels (``'None'`` /
        # blank) fall back to ``email`` / the IMAP password. Otherwise a probe
        # could pass while the real send fails (or vice versa).
        await smtp_test_login(
            host=smtp_host,
            port=smtp_port,
            ssl_on=smtp_ssl,
            starttls=smtp_starttls,
            username=normalize_optional_login(smtp_username) or email,
            password=normalize_optional_secret(smtp_password) or password,
        )
        return TestResult(imap_ok=True, smtp_ok=True)

    async def _test_existing_account(self, scope: VisibilityScope, account_id: int) -> TestResult:
        visible = await self._visible_user_ids(scope)
        acc = await self._repo.get_for_user_ids(visible, account_id)
        if acc is None:
            raise NotFoundError()

        if acc.auth_type == "oauth_outlook":
            return await self._test_oauth_account(acc)

        # Password account: re-probe with the stored credentials (same absence
        # semantics as the send path — see shared/credentials.py).
        assert acc.encrypted_password is not None
        imap_pwd = decrypt_mail_password(acc.encrypted_password, acc.id)
        stored_smtp_pwd: str | None = None
        if acc.smtp_encrypted_password is not None:
            stored_smtp_pwd = normalize_optional_secret(
                decrypt_mail_password(acc.smtp_encrypted_password, acc.id)
            )
        smtp_pwd: str = stored_smtp_pwd if stored_smtp_pwd is not None else imap_pwd
        return await self._test_credentials(
            email=acc.email,
            password=imap_pwd,
            imap_host=acc.imap_host,
            imap_port=acc.imap_port,
            imap_ssl=acc.imap_ssl,
            smtp_host=acc.smtp_host,
            smtp_port=acc.smtp_port,
            smtp_ssl=acc.smtp_ssl,
            smtp_starttls=acc.smtp_starttls,
            smtp_username=acc.smtp_username,
            smtp_password=smtp_pwd,
        )

    async def _test_oauth_account(self, acc: MailAccount) -> TestResult:
        """XOAUTH2 connectivity probe (P2) under the hard-deadline (ADR-0047 §1).

        Mirrors the send path: a needs-consent account is rejected with the
        documented 409 before any token refresh / network connect. Like P1, the
        ``wait_for`` sits inside the probe method, never at the call site
        (ADR-0047 §1/§1.1). The token refresh (a network call to Microsoft) is
        INSIDE the deadline — it is part of the probe.
        """
        if acc.oauth_needs_consent:
            raise OAuthReconsentRequiredError("Reconnect Outlook to test this account")
        stage = _ProbeStage("oauth_token")
        try:
            return await asyncio.wait_for(
                self._test_oauth_account_inner(acc, stage=stage),
                timeout=get_settings().MAILBOX_TEST_DEADLINE_SECONDS,
            )
        except TimeoutError as exc:
            raise _deadline_error(stage.value) from exc

    async def _test_oauth_account_inner(
        self, acc: MailAccount, *, stage: _ProbeStage
    ) -> TestResult:
        """Body of P2: refresh→access-token, then IMAP-XOAUTH2, then SMTP-XOAUTH2."""
        stage.value = "oauth_token"
        try:
            access_token = await OutlookTokenService(self._db).get_valid_access_token(acc)
        except OAuthRefreshInvalidError as exc:
            # The refresh token died between the consent and this probe — the
            # service has already flagged ``oauth_needs_consent``; surface the
            # documented 409 so the CRM prompts a reconnect (ADR-0025 §9.1).
            raise OAuthReconsentRequiredError("Reconnect Outlook to test this account") from exc
        stage.value = "imap"
        await imap_test_oauth(
            host=acc.imap_host,
            port=acc.imap_port,
            email=acc.email,
            access_token=access_token,
        )
        stage.value = "smtp"
        await smtp_test_oauth(
            host=acc.smtp_host,
            port=acc.smtp_port,
            starttls=acc.smtp_starttls,
            email=acc.email,
            access_token=access_token,
        )
        return TestResult(imap_ok=True, smtp_ok=True)

    # --- Create ------------------------------------------------------------

    async def _resolve_target_user_id(
        self,
        scope: VisibilityScope,
        target_user_id: int | None,
    ) -> int:
        """Resolve the row's owner ``user_id``.

        ADR-0044 §4 (phase A3): the role branches (group_leader / group_member)
        and the target-team validation (``_validate_target_group``, ADR-0031)
        went away with ``groups`` / ``mail_accounts.group_id``. One live path
        remains: the synthetic ``crm-service`` super_admin — the default owner
        is itself; an explicit ``target_user_id`` must exist.
        """
        if not scope.is_super_admin:
            raise ForbiddenError("Only the crm-service scope may manage mailboxes")
        if target_user_id is None:
            return scope.user_id
        target = await self._users.get_by_id(target_user_id)
        if target is None:
            raise NotFoundError()
        return target_user_id

    async def create(
        self,
        *,
        scope: VisibilityScope,
        payload: MailAccountCreateRequest,
    ) -> MailAccountDTO:
        target_user_id = await self._resolve_target_user_id(scope, payload.target_user_id)

        # With a single ``crm-service`` owner ``UNIQUE(user_id, email)`` is
        # effectively a GLOBAL uniqueness of the address (ADR-0043 §4).
        existing = await self._repo.find_by_user_email(target_user_id, payload.email)
        if existing is not None:
            raise ConflictError("Email already added", field="email")
        await self.test(payload.as_test_request())

        owner = await self._users.get_by_id(target_user_id)
        if owner is None:
            raise NotFoundError()

        new_id = await self._repo.next_account_id()
        encrypted = encrypt_mail_password(payload.password, new_id)
        smtp_encrypted: bytes | None = None
        if payload.smtp_password:
            smtp_encrypted = encrypt_mail_password(payload.smtp_password, new_id)

        try:
            acc = await self._repo.insert_with_id(
                account_id=new_id,
                user_id=target_user_id,
                email=payload.email,
                encrypted_password=encrypted,
                imap_host=payload.imap_host,
                imap_port=payload.imap_port,
                imap_ssl=payload.imap_ssl,
                smtp_host=payload.smtp_host,
                smtp_port=payload.smtp_port,
                smtp_ssl=payload.smtp_ssl,
                smtp_starttls=payload.smtp_starttls,
                smtp_username=payload.smtp_username,
                smtp_encrypted_password=smtp_encrypted,
                display_name=payload.display_name,
            )
        except IntegrityError as exc:
            raise ConflictError("Email already added", field="email") from exc

        return _to_dto(acc, owner)

    # --- Update ------------------------------------------------------------

    async def update(
        self,
        *,
        scope: VisibilityScope,
        account_id: int,
        payload: MailAccountUpdateRequest,
    ) -> MailAccountDTO:
        visible = await self._visible_user_ids(scope)
        acc = await self._repo.get_for_user_ids(visible, account_id)
        if acc is None:
            raise NotFoundError()

        # ADR-0025 §4c: oauth_outlook accounts have fixed Microsoft host/port
        # and token-based auth — only ``display_name`` may be edited. Any
        # attempt to *change* credentials / hosts / ports is rejected.
        #
        # The edit form is shared with password accounts and always submits a
        # full snapshot (host/port/ssl/starttls + display_name). Resubmitting a
        # field equal to the stored value is a no-op, not a credential change,
        # so a field counts as a forbidden change only when it is provided
        # (not None) AND differs from the account's current value. ``password``
        # / ``smtp_password`` are forbidden whenever a non-empty value is sent —
        # oauth accounts have no password to set.
        if acc.auth_type == "oauth_outlook":
            forbidden_changes = (
                (payload.email is not None and payload.email != acc.email)
                or bool(payload.password)
                or (payload.imap_host is not None and payload.imap_host != acc.imap_host)
                or (payload.imap_port is not None and payload.imap_port != acc.imap_port)
                or (payload.imap_ssl is not None and payload.imap_ssl != acc.imap_ssl)
                or (payload.smtp_host is not None and payload.smtp_host != acc.smtp_host)
                or (payload.smtp_port is not None and payload.smtp_port != acc.smtp_port)
                or (payload.smtp_ssl is not None and payload.smtp_ssl != acc.smtp_ssl)
                or (
                    payload.smtp_starttls is not None and payload.smtp_starttls != acc.smtp_starttls
                )
                or (
                    payload.smtp_username is not None and payload.smtp_username != acc.smtp_username
                )
                or bool(payload.smtp_password)
            )
            if forbidden_changes:
                raise ValidationError(
                    "OAuth accounts allow changing only the display name",
                    field="auth_type",
                )
            oauth_update_fields: dict[str, object] = {}
            if payload.clear_display_name:
                oauth_update_fields["display_name"] = None
            elif payload.display_name is not None:
                oauth_update_fields["display_name"] = payload.display_name
            if oauth_update_fields:
                await self._repo.update_fields(account_id, **oauth_update_fields)
            refreshed = await self._repo.get_by_id(account_id)
            assert refreshed is not None
            owner = await self._users.get_by_id(refreshed.user_id)
            assert owner is not None
            return _to_dto(refreshed, owner)

        new_email = payload.email or acc.email
        new_imap_host = payload.imap_host or acc.imap_host
        new_imap_port = payload.imap_port or acc.imap_port
        new_imap_ssl = payload.imap_ssl if payload.imap_ssl is not None else acc.imap_ssl
        new_smtp_host = payload.smtp_host or acc.smtp_host
        new_smtp_port = payload.smtp_port or acc.smtp_port
        new_smtp_ssl = payload.smtp_ssl if payload.smtp_ssl is not None else acc.smtp_ssl
        new_smtp_starttls = (
            payload.smtp_starttls if payload.smtp_starttls is not None else acc.smtp_starttls
        )
        # Both sides are normalised: the payload value (defence in depth — the
        # schema already scrubs it) and the STORED value, so a PATCH on a row
        # poisoned by a past import rewrites ``smtp_username`` as SQL NULL
        # instead of carrying the ``'None'`` text forward.
        new_smtp_username = (
            normalize_optional_login(payload.smtp_username)
            if payload.smtp_username is not None
            else normalize_optional_login(acc.smtp_username)
        )

        if new_smtp_ssl and new_smtp_starttls:
            raise ConflictError("smtp_ssl and smtp_starttls are mutually exclusive")

        # Decide whether to re-validate IMAP/SMTP credentials on this PATCH.
        # FE-FIX round-5 #4: re-test only when the user actually submits a
        # new IMAP or SMTP password. Editing a nickname (or even host/port)
        # without re-entering the password must not trigger a login probe —
        # the stored app-password may have expired, and a probe with a
        # stale password fails with 535 even though the user only renamed
        # the account. The next scheduled sync_cycle will surface real
        # connectivity problems naturally.
        creds_changed = bool(payload.password or payload.smtp_password)

        if creds_changed:
            if payload.password:
                imap_pwd = payload.password
            else:
                # password accounts always have a non-NULL encrypted_password
                # (DB CHECK ck_mail_accounts_password_creds); oauth accounts
                # already returned above.
                assert acc.encrypted_password is not None
                imap_pwd = decrypt_mail_password(acc.encrypted_password, acc.id)

            if payload.smtp_password:
                smtp_pwd = payload.smtp_password
            elif acc.smtp_encrypted_password is not None:
                smtp_pwd = decrypt_mail_password(acc.smtp_encrypted_password, acc.id)
            else:
                smtp_pwd = imap_pwd

            test_payload = MailAccountTestRequest(
                email=new_email,
                password=imap_pwd,
                imap_host=new_imap_host,
                imap_port=new_imap_port,
                imap_ssl=new_imap_ssl,
                smtp_host=new_smtp_host,
                smtp_port=new_smtp_port,
                smtp_ssl=new_smtp_ssl,
                smtp_starttls=new_smtp_starttls,
                smtp_username=new_smtp_username,
                smtp_password=smtp_pwd,
            )
            await self.test(test_payload)

        update_fields: dict[str, object] = {
            "email": new_email,
            "imap_host": new_imap_host,
            "imap_port": new_imap_port,
            "imap_ssl": new_imap_ssl,
            "smtp_host": new_smtp_host,
            "smtp_port": new_smtp_port,
            "smtp_ssl": new_smtp_ssl,
            "smtp_starttls": new_smtp_starttls,
            "smtp_username": new_smtp_username,
        }
        if payload.password:
            update_fields["encrypted_password"] = encrypt_mail_password(payload.password, acc.id)
        if payload.smtp_password:
            update_fields["smtp_encrypted_password"] = encrypt_mail_password(
                payload.smtp_password, acc.id
            )
        if payload.clear_display_name:
            update_fields["display_name"] = None
        elif payload.display_name is not None:
            update_fields["display_name"] = payload.display_name
        # Only flip ``is_active`` / clear sync-error state when we actually
        # re-validated credentials. A bare display_name edit must not reset
        # ``consecutive_failures`` — the account's IMAP/SMTP health hasn't
        # been re-verified, so leave the status unchanged.
        if creds_changed:
            update_fields["is_active"] = True
            update_fields["last_sync_error"] = None
            update_fields["consecutive_failures"] = 0
            # ADR-0033: re-enabling the mailbox (Disabled → Active) resets the
            # alert idempotency stamp so a *subsequent* auto-disable generates a
            # fresh alert (repeat only on an honest re-enable → disable).
            update_fields["disabled_alert_sent_at"] = None

        await self._repo.update_fields(account_id, **update_fields)
        refreshed = await self._repo.get_by_id(account_id)
        assert refreshed is not None
        # ADR-0046 §3 H5: on a credential re-enable (Disabled → Active) mirror
        # the mailbox status change to the CRM so it clears the down-alert. The
        # hook is DEFERRED — it fires in the router AFTER COMMIT (ADR-0046 §2),
        # never inside this still-open transaction.
        if creds_changed:
            self._pending_status_account_ids.append(account_id)
        owner = await self._users.get_by_id(refreshed.user_id)
        assert owner is not None
        return _to_dto(refreshed, owner)

    # --- Activate / deactivate --------------------------------------------

    async def set_active(
        self, *, scope: VisibilityScope, account_id: int, is_active: bool
    ) -> MailAccountDTO:
        """Toggle ``is_active`` on a visible mailbox (ADR-0039 §2 external PATCH).

        Distinct from :meth:`update` (which flips ``is_active=True`` only as a
        side effect of a credential change). On **activate** we mirror the
        credential re-enable branch (ADR-0033): reset ``last_sync_error`` /
        ``consecutive_failures`` and clear the alert idempotency stamp so a
        subsequent honest auto-disable re-alerts. On **deactivate** we only set
        the flag (a manual disable never stamps an alert).

        ADR-0046 §3 H6: the mailbox-status hook is DEFERRED to
        :meth:`flush_crm_status_events` (fired by the router after COMMIT). A
        deactivation is the one status change that can NEVER be re-derived: the
        box drops out of ``list_active()``, so no further sync cycle — and no
        further status event — will ever run for it. Enqueuing before COMMIT
        could ship ``is_active=true`` to the CRM and stick forever.
        """
        visible = await self._visible_user_ids(scope)
        acc = await self._repo.get_for_user_ids(visible, account_id)
        if acc is None:
            raise NotFoundError()
        update_fields: dict[str, object] = {"is_active": is_active}
        if is_active:
            update_fields["last_sync_error"] = None
            update_fields["consecutive_failures"] = 0
            update_fields["disabled_alert_sent_at"] = None
        await self._repo.update_fields(account_id, **update_fields)
        refreshed = await self._repo.get_by_id(account_id)
        assert refreshed is not None
        # ADR-0046 §3 H6: mirror the mailbox status change (activate /
        # deactivate) to the CRM. On activate this clears the CRM down-alert; on
        # deactivate it records the manual disable. Deferred to after COMMIT.
        self._pending_status_account_ids.append(account_id)
        owner = await self._users.get_by_id(refreshed.user_id)
        assert owner is not None
        return _to_dto(refreshed, owner)

    # --- Delete ------------------------------------------------------------

    async def delete(self, *, scope: VisibilityScope, account_id: int) -> None:
        visible = await self._visible_user_ids(scope)
        acc = await self._repo.get_for_user_ids(visible, account_id)
        if acc is None:
            raise NotFoundError()
        # ADR-0044 §4 (phase A3 → G): the MinIO attachment-delete cascade is
        # gone — attachments are neither fetched nor stored (ADR-0043 §4). The
        # ``messages`` rows go away via the FK CASCADE.
        await self._repo.delete(account_id)

    # --- Force sync marker -------------------------------------------------

    async def force_sync(self, *, scope: VisibilityScope, account_id: int) -> None:
        visible = await self._visible_user_ids(scope)
        acc = await self._repo.get_for_user_ids(visible, account_id)
        if acc is None:
            raise NotFoundError()
        redis = get_redis()
        await redis.set(f"force_sync:{account_id}", "1", ex=60)
        log.info(
            "force_sync_marked",
            actor_user_id=scope.user_id,
            mail_account_id=account_id,
            owner_user_id=acc.user_id,
        )
