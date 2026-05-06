"""MailAccountService — CRUD + test-login + force-sync marker."""

from __future__ import annotations

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.accounts.schemas import (
    MailAccountCreateRequest,
    MailAccountDTO,
    MailAccountTestRequest,
    MailAccountUpdateRequest,
    TestResult,
)
from backend.app.accounts.testers import imap_test_login, smtp_test_login
from backend.app.exceptions import (
    ConflictError,
    NotFoundError,
)
from backend.app.repositories.mail_accounts import MailAccountsRepo
from backend.app.repositories.messages import MessagesRepo
from shared.crypto import decrypt_mail_password, encrypt_mail_password
from shared.logging import get_logger
from shared.models import MailAccount
from shared.redis_client import get_redis
from shared.storage import get_storage

log = get_logger(__name__)


def _to_dto(acc: MailAccount) -> MailAccountDTO:
    return MailAccountDTO(
        id=acc.id,
        email=acc.email,
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
        self._storage = get_storage()

    # --- Reads -------------------------------------------------------------

    async def list_for_user(self, user_id: int) -> list[MailAccountDTO]:
        rows = await self._repo.list_for_user(user_id)
        return [_to_dto(r) for r in rows]

    async def get_for_user(self, user_id: int, account_id: int) -> MailAccountDTO:
        acc = await self._repo.get_for_user(user_id, account_id)
        if acc is None:
            raise NotFoundError()
        return _to_dto(acc)

    # --- Test login --------------------------------------------------------

    async def test(self, payload: MailAccountTestRequest) -> TestResult:
        # IMAP
        await imap_test_login(
            host=payload.imap_host,
            port=payload.imap_port,
            ssl_on=payload.imap_ssl,
            username=payload.email,
            password=payload.password,
        )
        # SMTP
        smtp_user = payload.smtp_username or payload.email
        smtp_pwd = payload.smtp_password or payload.password
        await smtp_test_login(
            host=payload.smtp_host,
            port=payload.smtp_port,
            ssl_on=payload.smtp_ssl,
            starttls=payload.smtp_starttls,
            username=smtp_user,
            password=smtp_pwd,
        )
        return TestResult(imap_ok=True, smtp_ok=True)

    # --- Create ------------------------------------------------------------

    async def create(self, *, user_id: int, payload: MailAccountCreateRequest) -> MailAccountDTO:
        # 1. Conflict check (cheap, before any IMAP round-trip).
        existing = await self._repo.find_by_user_email(user_id, payload.email)
        if existing is not None:
            raise ConflictError("Email already added", field="email")
        # 2. Test IMAP + SMTP. Raises 422 on failure.
        await self.test(payload)

        # 3. Reserve id, encrypt with that id in AAD, INSERT.
        new_id = await self._repo.next_account_id()
        encrypted = encrypt_mail_password(payload.password, new_id)
        smtp_encrypted: bytes | None = None
        if payload.smtp_password:
            smtp_encrypted = encrypt_mail_password(payload.smtp_password, new_id)

        try:
            acc = await self._repo.insert_with_id(
                account_id=new_id,
                user_id=user_id,
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
            )
        except IntegrityError as exc:
            # Race with another concurrent add — surface as 409.
            raise ConflictError("Email already added", field="email") from exc
        return _to_dto(acc)

    # --- Update ------------------------------------------------------------

    async def update(
        self,
        *,
        user_id: int,
        account_id: int,
        payload: MailAccountUpdateRequest,
    ) -> MailAccountDTO:
        acc = await self._repo.get_for_user(user_id, account_id)
        if acc is None:
            raise NotFoundError()

        # Build the to-be-tested credentials. Apply incoming fields on top of
        # current ones; we always re-test if any auth-relevant field changed.
        # Determine effective credentials and connection params after patch.
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

        # IMAP password: if user supplied a new one, use it; else decrypt the
        # stored blob.
        if payload.password:
            imap_pwd = payload.password
        else:
            imap_pwd = decrypt_mail_password(acc.encrypted_password, acc.id)

        # SMTP password: if explicit smtp_password sent, use it; else stored
        # smtp blob; else fall back to imap password.
        if payload.smtp_password:
            smtp_pwd = payload.smtp_password
        elif acc.smtp_encrypted_password is not None:
            smtp_pwd = decrypt_mail_password(acc.smtp_encrypted_password, acc.id)
        else:
            smtp_pwd = imap_pwd

        # Always re-test on PATCH per ``docs/04-api-contracts.md``.
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

        # Build update fields.
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
        # If account was disabled and it now passes test — re-enable.
        update_fields["is_active"] = True
        update_fields["last_sync_error"] = None
        update_fields["consecutive_failures"] = 0

        await self._repo.update_fields(account_id, **update_fields)
        # Reload row.
        refreshed = await self._repo.get_for_user(user_id, account_id)
        assert refreshed is not None
        return _to_dto(refreshed)

    # --- Delete ------------------------------------------------------------

    async def delete(self, *, user_id: int, account_id: int) -> None:
        acc = await self._repo.get_for_user(user_id, account_id)
        if acc is None:
            raise NotFoundError()
        # Collect S3 keys for deletion BEFORE the cascade.
        keys = await self._messages.select_attachment_keys_for_account(account_id)
        # Delete the row -> cascade removes messages + attachments.
        await self._repo.delete(account_id)
        # Drop blobs (best-effort; orphan_scan = TD-004).
        if keys:
            await self._storage.delete_objects(keys)
        # Also clean up any straggler objects under the account prefix.
        prefix = f"{user_id}/{account_id}/"
        await self._storage.delete_prefix(prefix)

    # --- Force sync marker -------------------------------------------------

    async def force_sync(self, *, user_id: int, account_id: int) -> None:
        acc = await self._repo.get_for_user(user_id, account_id)
        if acc is None:
            raise NotFoundError()
        redis = get_redis()
        # 60s TTL — worker will pick this up on its next 5-minute tick.
        await redis.set(f"force_sync:{account_id}", "1", ex=60)
        log.info(
            "force_sync_marked",
            user_id=user_id,
            mail_account_id=account_id,
        )
