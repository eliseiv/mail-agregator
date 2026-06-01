"""MailAccountService — CRUD + test-login + force-sync marker.

Post-ADR-0019: visibility is governed by :class:`VisibilityScope`. The
caller's user_id is no longer the only key — group leaders / members
share the same view of every member's mailboxes (ADR-0019 §7.1) and a
super-admin sees all.
"""

from __future__ import annotations

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

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
from backend.app.deps import VisibilityScope
from backend.app.exceptions import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
    OAuthReconsentRequiredError,
    ValidationError,
)
from backend.app.oauth.service import OAuthRefreshInvalidError, OutlookTokenService
from backend.app.repositories.mail_accounts import MailAccountsRepo
from backend.app.repositories.messages import MessagesRepo
from backend.app.repositories.users import UsersRepo
from shared.crypto import decrypt_mail_password, encrypt_mail_password
from shared.logging import get_logger
from shared.models import MailAccount, User
from shared.redis_client import get_redis
from shared.storage import get_storage

log = get_logger(__name__)


def _require(value: str | None, field: str) -> str:
    """Narrow an optional credential field to ``str`` for the ad-hoc test path.

    The :class:`MailAccountTestRequest` validator already guarantees these are
    present when ``account_id`` is unset; this keeps mypy honest at the call
    site and degrades to a clear 400 if a caller bypasses the schema.
    """
    if not value:
        raise ValidationError(f"{field} is required", field=field)
    return value


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
        smtp_username=acc.smtp_username,
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
        self._messages = MessagesRepo(session)
        self._users = UsersRepo(session)
        self._storage = get_storage()

    # --- Visibility helpers ------------------------------------------------

    async def visible_user_ids(self, scope: VisibilityScope) -> list[int] | None:
        """Compute the set of ``mail_accounts.id`` visible to the caller.

        FE-FIX round-10: the filter shifted from per-user to per-account
        — visibility is determined by ``mail_accounts.group_id`` (with a
        personal exception for the owner). The legacy method name is
        preserved to avoid renaming every caller.

        ``None`` = "no scope filter" (super-admin path).
        ``[]``   = nothing visible.
        ``[id…]`` = the explicit list of visible ``mail_accounts.id``.
        """
        if scope.is_super_admin:
            return None
        return await self._repo.list_account_ids_visible(
            group_id=scope.group_id, owner_user_id=scope.user_id
        )

    async def _visible_user_ids(self, scope: VisibilityScope) -> list[int] | None:
        return await self.visible_user_ids(scope)

    # --- Reads -------------------------------------------------------------

    async def list_for_scope(self, scope: VisibilityScope) -> list[MailAccountDTO]:
        # FE-FIX round-10: visibility now keys off ``mail_accounts.group_id``
        # (set on insert from the owner's then-current group, never moved
        # automatically when the owner changes group). Personal accounts
        # remain visible to their owner via the user_id condition.
        if scope.is_super_admin:
            rows = await self._repo.list_all()
            # Round-18: collapse duplicates by lower(email). Two teams may
            # add the same mailbox independently — super-admin sees ONE row
            # per email (canonical = lowest mail_account.id). Team views
            # are naturally scoped by group_id, so they only see their own.
            seen: set[str] = set()
            deduped: list[MailAccount] = []
            for a in sorted(rows, key=lambda r: r.id):
                key = a.email.lower()
                if key in seen:
                    continue
                seen.add(key)
                deduped.append(a)
            rows = deduped
        else:
            rows = await self._repo.list_for_group_or_owner(
                group_id=scope.group_id, owner_user_id=scope.user_id
            )
        owner_ids = sorted({a.user_id for a in rows})
        owner_map = await self._users.get_many_by_ids(owner_ids)
        return [_to_dto(a, owner_map[a.user_id]) for a in rows if a.user_id in owner_map]

    async def get_for_scope(self, scope: VisibilityScope, account_id: int) -> MailAccountDTO:
        if scope.is_super_admin:
            acc = await self._repo.get_by_id(account_id)
        else:
            acc = await self._repo.get_for_group_or_owner(
                group_id=scope.group_id,
                owner_user_id=scope.user_id,
                account_id=account_id,
            )
        if acc is None:
            raise NotFoundError()
        owner = await self._users.get_by_id(acc.user_id)
        if owner is None:
            # FK should prevent this; surface 404 to avoid 500.
            raise NotFoundError()
        return _to_dto(acc, owner)

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
        await imap_test_login(
            host=imap_host,
            port=imap_port,
            ssl_on=imap_ssl,
            username=email,
            password=password,
        )
        await smtp_test_login(
            host=smtp_host,
            port=smtp_port,
            ssl_on=smtp_ssl,
            starttls=smtp_starttls,
            username=smtp_username or email,
            password=smtp_password or password,
        )
        return TestResult(imap_ok=True, smtp_ok=True)

    async def _test_existing_account(self, scope: VisibilityScope, account_id: int) -> TestResult:
        visible = await self._visible_user_ids(scope)
        acc = await self._repo.get_for_user_ids(visible, account_id)
        if acc is None:
            raise NotFoundError()

        if acc.auth_type == "oauth_outlook":
            return await self._test_oauth_account(acc)

        # Password account: re-probe with the stored credentials.
        assert acc.encrypted_password is not None
        imap_pwd = decrypt_mail_password(acc.encrypted_password, acc.id)
        if acc.smtp_encrypted_password is not None:
            smtp_pwd: str = decrypt_mail_password(acc.smtp_encrypted_password, acc.id)
        else:
            smtp_pwd = imap_pwd
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
        """XOAUTH2 connectivity probe for an oauth_outlook account (ADR-0025 §4).

        Mirrors the send path: a needs-consent account is rejected with the
        documented 409 before any token refresh / network connect.
        """
        if acc.oauth_needs_consent:
            raise OAuthReconsentRequiredError("Reconnect Outlook to test this account")
        try:
            access_token = await OutlookTokenService(self._db).get_valid_access_token(acc)
        except OAuthRefreshInvalidError as exc:
            # The refresh token died between the consent and this probe — the
            # service has already flagged ``oauth_needs_consent``; surface the
            # documented 409 so the UI prompts a reconnect (ADR-0025 §9.1).
            raise OAuthReconsentRequiredError("Reconnect Outlook to test this account") from exc
        await imap_test_oauth(
            host=acc.imap_host,
            port=acc.imap_port,
            email=acc.email,
            access_token=access_token,
        )
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
        """Apply ADR-0019 §8 rules and return the row's owner user_id."""
        # group_member: cannot create on someone else.
        if scope.is_group_member:
            if target_user_id is not None and target_user_id != scope.user_id:
                raise ValidationError(
                    "target_user_id must equal own user_id for group_member",
                    field="target_user_id",
                )
            return scope.user_id

        # group_leader: target must be in the same group; default = self.
        if scope.is_group_leader:
            if target_user_id is None:
                return scope.user_id
            target = await self._users.get_by_id(target_user_id)
            if target is None:
                raise NotFoundError()
            if target.group_id != scope.group_id:
                raise ForbiddenError("user_not_in_group_scope")
            return target_user_id

        # super_admin: target must exist (default = self).
        if scope.is_super_admin:
            if target_user_id is None:
                return scope.user_id
            target = await self._users.get_by_id(target_user_id)
            if target is None:
                raise NotFoundError()
            return target_user_id

        # Defensive — unknown role.
        raise ForbiddenError()

    async def create(
        self,
        *,
        scope: VisibilityScope,
        payload: MailAccountCreateRequest,
    ) -> MailAccountDTO:
        target_user_id = await self._resolve_target_user_id(scope, payload.target_user_id)

        # Round-18: revert the global guard. Two teams MAY add the same
        # mailbox independently — each gets its own credentials and its own
        # ``mail_account.id``. Duplicates are hidden later at read-time by
        # ``list_for_scope`` / message visibility (super-admin sees the
        # canonical row per email; teams see only their own).
        existing = await self._repo.find_by_user_email(target_user_id, payload.email)
        if existing is not None:
            raise ConflictError("Email already added", field="email")
        await self.test(payload.as_test_request())

        # FE-FIX round-10: bind the new account to the owner's CURRENT
        # group at insert time. Subsequent owner-group changes do NOT move
        # the account (see ``MailAccountsRepo.attach_orphans_to_group`` for
        # the orphan-attach exception when going from "no group" to a real
        # group via PATCH /api/admin/users).
        owner = await self._users.get_by_id(target_user_id)
        if owner is None:
            raise NotFoundError()
        owner_group_id = owner.group_id

        new_id = await self._repo.next_account_id()
        encrypted = encrypt_mail_password(payload.password, new_id)
        smtp_encrypted: bytes | None = None
        if payload.smtp_password:
            smtp_encrypted = encrypt_mail_password(payload.smtp_password, new_id)

        try:
            acc = await self._repo.insert_with_id(
                account_id=new_id,
                user_id=target_user_id,
                group_id=owner_group_id,
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
        new_smtp_username = (
            payload.smtp_username if payload.smtp_username is not None else acc.smtp_username
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

        await self._repo.update_fields(account_id, **update_fields)
        refreshed = await self._repo.get_by_id(account_id)
        assert refreshed is not None
        owner = await self._users.get_by_id(refreshed.user_id)
        assert owner is not None
        return _to_dto(refreshed, owner)

    # --- Delete ------------------------------------------------------------

    async def delete(self, *, scope: VisibilityScope, account_id: int) -> None:
        visible = await self._visible_user_ids(scope)
        acc = await self._repo.get_for_user_ids(visible, account_id)
        if acc is None:
            raise NotFoundError()
        keys = await self._messages.select_attachment_keys_for_account(account_id)
        await self._repo.delete(account_id)
        if keys:
            await self._storage.delete_objects(keys)
        prefix = f"{acc.user_id}/{account_id}/"
        await self._storage.delete_prefix(prefix)

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
