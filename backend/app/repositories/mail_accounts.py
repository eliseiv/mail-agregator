"""Repository for ``mail_accounts``.

Special method :meth:`MailAccountsRepo.next_account_id` reserves the next
``BIGSERIAL`` so callers can build the AAD-bound ciphertext before INSERT
(see ``docs/05-modules.md`` sec. 5 / ADR-0005).
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models import MailAccount


class MailAccountsRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    # --- Reads -------------------------------------------------------------

    async def get_by_id(self, account_id: int) -> MailAccount | None:
        return await self._s.get(MailAccount, account_id)

    async def get_for_user(self, user_id: int, account_id: int) -> MailAccount | None:
        stmt = select(MailAccount).where(
            MailAccount.id == account_id, MailAccount.user_id == user_id
        )
        return (await self._s.execute(stmt)).scalar_one_or_none()

    async def get_for_user_ids(
        self, user_ids: list[int] | None, account_id: int
    ) -> MailAccount | None:
        """Visibility-aware get.

        ``user_ids=None`` means "no scope filter" (super-admin path).
        ``user_ids=[]`` means "no users visible" — always returns ``None``.
        """
        if user_ids is None:
            return await self.get_by_id(account_id)
        if not user_ids:
            return None
        stmt = select(MailAccount).where(
            MailAccount.id == account_id, MailAccount.user_id.in_(user_ids)
        )
        return (await self._s.execute(stmt)).scalar_one_or_none()

    async def list_for_user(self, user_id: int) -> list[MailAccount]:
        stmt = select(MailAccount).where(MailAccount.user_id == user_id).order_by(MailAccount.id)
        return list((await self._s.execute(stmt)).scalars().all())

    async def list_for_user_ids(self, user_ids: list[int]) -> list[MailAccount]:
        """Mail accounts for a set of users (visibility-scope aware).

        Returns a flat list ordered by ``(user_id, id)``. Used by
        :class:`backend.app.accounts.service.MailAccountService` to build
        the list response for super-admin and group leaders/members.
        """
        if not user_ids:
            return []
        stmt = (
            select(MailAccount)
            .where(MailAccount.user_id.in_(user_ids))
            .order_by(MailAccount.user_id, MailAccount.id)
        )
        return list((await self._s.execute(stmt)).scalars().all())

    async def list_all(self) -> list[MailAccount]:
        """All mail accounts (super-admin only). No pagination — small Ns."""
        stmt = select(MailAccount).order_by(MailAccount.user_id, MailAccount.id)
        return list((await self._s.execute(stmt)).scalars().all())

    async def list_for_users(self, user_ids: list[int]) -> dict[int, list[MailAccount]]:
        """Bulk-load mail accounts for many users to avoid N+1.

        Returns a dict mapping ``user_id`` -> list of accounts (possibly empty).
        """
        if not user_ids:
            return {}
        stmt = (
            select(MailAccount)
            .where(MailAccount.user_id.in_(user_ids))
            .order_by(MailAccount.user_id, MailAccount.id)
        )
        out: dict[int, list[MailAccount]] = {uid: [] for uid in user_ids}
        for acc in (await self._s.execute(stmt)).scalars():
            out[acc.user_id].append(acc)
        return out

    async def list_active(self) -> list[MailAccount]:
        """All active accounts for the worker sync cycle, oldest-synced first."""
        stmt = (
            select(MailAccount)
            .where(MailAccount.is_active.is_(True))
            .order_by(MailAccount.last_synced_at.asc().nulls_first(), MailAccount.id)
        )
        return list((await self._s.execute(stmt)).scalars().all())

    async def list_active_by_ids(self, account_ids: list[int]) -> list[MailAccount]:
        """Active accounts whose id is in ``account_ids``.

        Used by the worker's ``force_sync_dispatch`` to fetch only the
        accounts that have a Redis ``force_sync:{id}`` marker, instead of
        loading every active row in the table on every dispatcher tick.
        Inactive accounts are filtered out — a force-sync marker for a
        disabled account is silently dropped (the marker itself is removed
        by :func:`worker.app.sync_cycle._drain_forced_account_ids`).
        """
        if not account_ids:
            return []
        stmt = (
            select(MailAccount)
            .where(
                MailAccount.is_active.is_(True),
                MailAccount.id.in_(account_ids),
            )
            .order_by(MailAccount.id)
        )
        return list((await self._s.execute(stmt)).scalars().all())

    async def find_by_user_email(self, user_id: int, email: str) -> MailAccount | None:
        stmt = select(MailAccount).where(
            MailAccount.user_id == user_id,
            func.lower(MailAccount.email) == email.lower(),
        )
        return (await self._s.execute(stmt)).scalar_one_or_none()

    # --- Writes ------------------------------------------------------------

    async def next_account_id(self) -> int:
        """``SELECT nextval('mail_accounts_id_seq')``.

        Used to predict the BIGSERIAL id so the encrypted password's AAD can
        bind to it (ADR-0005).
        """
        row = await self._s.execute(text("SELECT nextval('mail_accounts_id_seq')"))
        return int(row.scalar_one())

    async def insert_with_id(
        self,
        *,
        account_id: int,
        user_id: int,
        email: str,
        encrypted_password: bytes,
        imap_host: str,
        imap_port: int,
        imap_ssl: bool,
        smtp_host: str,
        smtp_port: int,
        smtp_ssl: bool,
        smtp_starttls: bool,
        smtp_username: str | None,
        smtp_encrypted_password: bytes | None,
        display_name: str | None = None,
    ) -> MailAccount:
        acc = MailAccount(
            id=account_id,
            user_id=user_id,
            email=email,
            display_name=display_name,
            encrypted_password=encrypted_password,
            imap_host=imap_host,
            imap_port=imap_port,
            imap_ssl=imap_ssl,
            smtp_host=smtp_host,
            smtp_port=smtp_port,
            smtp_ssl=smtp_ssl,
            smtp_starttls=smtp_starttls,
            smtp_username=smtp_username,
            smtp_encrypted_password=smtp_encrypted_password,
            is_active=True,
            consecutive_failures=0,
        )
        self._s.add(acc)
        await self._s.flush()
        await self._s.refresh(acc)
        return acc

    async def update_fields(self, account_id: int, **fields: object) -> None:
        if not fields:
            return
        fields["updated_at"] = datetime.now(UTC)
        await self._s.execute(
            update(MailAccount).where(MailAccount.id == account_id).values(**fields)
        )

    async def delete(self, account_id: int) -> None:
        await self._s.execute(delete(MailAccount).where(MailAccount.id == account_id))

    # --- Sync state mutations (worker) ------------------------------------

    async def mark_sync_success(
        self,
        account_id: int,
        *,
        last_synced_uidnext: int | None,
        last_uidvalidity: int | None,
    ) -> None:
        await self._s.execute(
            update(MailAccount)
            .where(MailAccount.id == account_id)
            .values(
                last_synced_uidnext=last_synced_uidnext,
                last_uidvalidity=last_uidvalidity,
                last_synced_at=datetime.now(UTC),
                last_sync_error=None,
                consecutive_failures=0,
                updated_at=datetime.now(UTC),
            )
        )

    async def mark_sync_failure(
        self,
        account_id: int,
        *,
        error: str,
        disable: bool = False,
    ) -> int:
        """Bump ``consecutive_failures``; optionally flip ``is_active=false``.

        Returns the new ``consecutive_failures`` value.
        """
        values: dict[str, object] = {
            "consecutive_failures": MailAccount.consecutive_failures + 1,
            "last_sync_error": error[:500],
            "last_synced_at": datetime.now(UTC),
            "updated_at": datetime.now(UTC),
        }
        if disable:
            values["is_active"] = False
        stmt = (
            update(MailAccount)
            .where(MailAccount.id == account_id)
            .values(**values)
            .returning(MailAccount.consecutive_failures)
        )
        row = (await self._s.execute(stmt)).one_or_none()
        return int(row[0]) if row else 0
