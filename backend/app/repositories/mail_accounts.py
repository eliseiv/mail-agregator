"""Repository for ``mail_accounts``.

Special method :meth:`MailAccountsRepo.next_account_id` reserves the next
``BIGSERIAL`` so callers can build the AAD-bound ciphertext before INSERT
(see ``docs/05-modules.md`` sec. 5 / ADR-0005).
"""

from __future__ import annotations

from collections.abc import Collection
from datetime import UTC, datetime

from sqlalchemy import and_, delete, func, or_, select, text, update
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
        self, mail_account_ids: list[int] | None, account_id: int
    ) -> MailAccount | None:
        """Visibility-aware get.

        ``mail_account_ids=None`` means "no scope filter" (super-admin).
        ``mail_account_ids=[]`` means nothing visible — returns ``None``.
        FE-FIX round-10: the filter shifted from ``MailAccount.user_id``
        to ``MailAccount.id`` so visibility follows ``mail_accounts.group_id``.
        """
        if mail_account_ids is None:
            return await self.get_by_id(account_id)
        if not mail_account_ids:
            return None
        stmt = select(MailAccount).where(
            MailAccount.id == account_id, MailAccount.id.in_(mail_account_ids)
        )
        return (await self._s.execute(stmt)).scalar_one_or_none()

    async def list_for_user(self, user_id: int) -> list[MailAccount]:
        stmt = select(MailAccount).where(MailAccount.user_id == user_id).order_by(MailAccount.id)
        return list((await self._s.execute(stmt)).scalars().all())

    async def list_for_user_ids(self, user_ids: list[int]) -> list[MailAccount]:
        """Mail accounts for a set of users (legacy helper).

        Returns a flat list ordered by ``(user_id, id)``. Pre round-10 this
        was the visibility helper for non-super_admin callers; today it
        survives as a generic "by-owner" lookup (used by group-rendering
        on the admin page when we want every account of every member of a
        group, regardless of where the account currently belongs).
        """
        if not user_ids:
            return []
        stmt = (
            select(MailAccount)
            .where(MailAccount.user_id.in_(user_ids))
            .order_by(MailAccount.user_id, MailAccount.id)
        )
        return list((await self._s.execute(stmt)).scalars().all())

    async def list_by_ids(self, account_ids: list[int]) -> list[MailAccount]:
        """Bulk-load accounts by their primary key. FE-FIX round-10."""
        if not account_ids:
            return []
        stmt = (
            select(MailAccount)
            .where(MailAccount.id.in_(account_ids))
            .order_by(MailAccount.user_id, MailAccount.id)
        )
        return list((await self._s.execute(stmt)).scalars().all())

    async def list_all(self) -> list[MailAccount]:
        """All mail accounts (super-admin only). No pagination — small Ns."""
        stmt = select(MailAccount).order_by(MailAccount.user_id, MailAccount.id)
        return list((await self._s.execute(stmt)).scalars().all())

    async def list_for_group_or_owner(
        self, *, group_ids: Collection[int] | None, owner_user_id: int
    ) -> list[MailAccount]:
        """Visibility query for a non-super_admin caller (FE-FIX round-10;
        ADR-0030 multi-group).

        Returns accounts that satisfy ANY of the following:
          - ``mail_accounts.group_id = ANY(group_ids)`` (accounts of any team
            the caller is a member of — ADR-0030 replaced the single
            ``users.group_id = mail_accounts.group_id`` predicate);
          - ``mail_accounts.user_id == owner_user_id`` (the caller's personal
            accounts — covers the case where the caller owns an account that
            was created while they had no group, so its ``group_id`` is
            still NULL or no longer matches any current membership).

        Pass an empty / ``None`` ``group_ids`` for callers without any team:
        only personal accounts are returned.
        """
        cond = MailAccount.user_id == owner_user_id
        if group_ids:
            cond = or_(cond, MailAccount.group_id.in_(list(group_ids)))
        stmt = select(MailAccount).where(cond).order_by(MailAccount.user_id, MailAccount.id)
        return list((await self._s.execute(stmt)).scalars().all())

    async def get_for_group_or_owner(
        self, *, group_ids: Collection[int] | None, owner_user_id: int, account_id: int
    ) -> MailAccount | None:
        owner_cond = MailAccount.user_id == owner_user_id
        if group_ids:
            visibility = or_(owner_cond, MailAccount.group_id.in_(list(group_ids)))
        else:
            visibility = owner_cond
        stmt = select(MailAccount).where(and_(MailAccount.id == account_id, visibility))
        return (await self._s.execute(stmt)).scalar_one_or_none()

    async def list_account_ids_visible(
        self, *, group_ids: Collection[int] | None, owner_user_id: int
    ) -> list[int]:
        """``mail_accounts.id`` visible to a non-super_admin caller (ADR-0030)."""
        cond = MailAccount.user_id == owner_user_id
        if group_ids:
            cond = or_(cond, MailAccount.group_id.in_(list(group_ids)))
        stmt = select(MailAccount.id).where(cond)
        return [int(r[0]) for r in (await self._s.execute(stmt)).all()]

    async def list_account_ids_in_group(self, group_id: int) -> list[int]:
        """``mail_accounts.id`` belonging to a group (super-admin filter)."""
        stmt = select(MailAccount.id).where(MailAccount.group_id == group_id)
        return [int(r[0]) for r in (await self._s.execute(stmt)).all()]

    async def list_canonical_account_ids(self) -> list[int]:
        """One canonical ``mail_accounts.id`` per ``LOWER(email)`` (round-18).

        Used by super-admin "all teams" views to hide duplicate IMAP polls
        when two teams independently added the same mailbox. We keep the
        oldest row (``MIN(id)``) as canonical — deterministic and stable
        across requests.
        """
        stmt = select(func.min(MailAccount.id)).group_by(func.lower(MailAccount.email))
        return [int(r[0]) for r in (await self._s.execute(stmt)).all()]

    async def attach_orphans_to_group(self, *, user_id: int, group_id: int) -> None:
        """Backfill ``mail_accounts.group_id`` for the user's orphan accounts.

        Called by :meth:`AdminService.update_user` when a user is moved
        from "no group" into a real group. Existing accounts that already
        have a ``group_id`` are NOT touched — those stay with their
        original group even when the owner changes group.
        """
        await self._s.execute(
            update(MailAccount)
            .where(
                MailAccount.user_id == user_id,
                MailAccount.group_id.is_(None),
            )
            .values(group_id=group_id, updated_at=datetime.now(UTC))
        )

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

    async def find_any_by_email(self, email: str) -> MailAccount | None:
        """Find ANY mail_account with this email (case-insensitive), regardless of owner.

        Round-16 bug fix: the historical ``UNIQUE (user_id, email)`` constraint
        allows the same address to be added by two different users (e.g. into
        two teams). Worker ``sync_cycle`` then polls IMAP independently for
        each row, inserts duplicate ``messages`` rows, and the Inbox shows
        every email twice — also resulting in duplicate auto-tags. Service
        layer uses this method to reject duplicates before SMTP/IMAP probe.
        """
        stmt = select(MailAccount).where(func.lower(MailAccount.email) == email.lower())
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
        group_id: int | None,
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
            group_id=group_id,
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

    async def insert_oauth_account_with_id(
        self,
        *,
        account_id: int,
        user_id: int,
        group_id: int | None,
        email: str,
        oauth_provider: str,
        oauth_refresh_token_encrypted: bytes,
        oauth_access_token_encrypted: bytes | None,
        oauth_access_token_expires_at: datetime | None,
        oauth_scopes: str | None,
        imap_host: str,
        imap_port: int,
        imap_ssl: bool,
        smtp_host: str,
        smtp_port: int,
        smtp_ssl: bool,
        smtp_starttls: bool,
        display_name: str | None = None,
    ) -> MailAccount:
        """Insert an ``auth_type='oauth_outlook'`` row (ADR-0025).

        ``encrypted_password`` stays NULL (oauth accounts have no password);
        the DB CHECK ``ck_mail_accounts_oauth_creds`` enforces a non-NULL
        ``oauth_refresh_token_encrypted`` + ``oauth_provider='outlook'``.
        """
        acc = MailAccount(
            id=account_id,
            user_id=user_id,
            group_id=group_id,
            email=email,
            display_name=display_name,
            encrypted_password=None,
            auth_type="oauth_outlook",
            oauth_provider=oauth_provider,
            oauth_refresh_token_encrypted=oauth_refresh_token_encrypted,
            oauth_access_token_encrypted=oauth_access_token_encrypted,
            oauth_access_token_expires_at=oauth_access_token_expires_at,
            oauth_needs_consent=False,
            oauth_scopes=oauth_scopes,
            imap_host=imap_host,
            imap_port=imap_port,
            imap_ssl=imap_ssl,
            smtp_host=smtp_host,
            smtp_port=smtp_port,
            smtp_ssl=smtp_ssl,
            smtp_starttls=smtp_starttls,
            smtp_username=None,
            smtp_encrypted_password=None,
            is_active=True,
            consecutive_failures=0,
        )
        self._s.add(acc)
        await self._s.flush()
        await self._s.refresh(acc)
        return acc

    async def update_oauth_tokens(
        self,
        account_id: int,
        *,
        oauth_refresh_token_encrypted: bytes | None = None,
        oauth_access_token_encrypted: bytes | None = None,
        oauth_access_token_expires_at: datetime | None = None,
        oauth_scopes: str | None = None,
        oauth_needs_consent: bool | None = None,
    ) -> None:
        """Persist refreshed OAuth tokens / consent state (ADR-0025 §3).

        Only non-``None`` keyword arguments are written, so a plain
        access-token refresh does not clobber the stored refresh token unless
        Microsoft rotated it.
        """
        values: dict[str, object] = {"updated_at": datetime.now(UTC)}
        if oauth_refresh_token_encrypted is not None:
            values["oauth_refresh_token_encrypted"] = oauth_refresh_token_encrypted
        if oauth_access_token_encrypted is not None:
            values["oauth_access_token_encrypted"] = oauth_access_token_encrypted
        if oauth_access_token_expires_at is not None:
            values["oauth_access_token_expires_at"] = oauth_access_token_expires_at
        if oauth_scopes is not None:
            values["oauth_scopes"] = oauth_scopes
        if oauth_needs_consent is not None:
            values["oauth_needs_consent"] = oauth_needs_consent
        await self._s.execute(
            update(MailAccount).where(MailAccount.id == account_id).values(**values)
        )

    async def mark_oauth_needs_consent(self, account_id: int) -> None:
        """Flag an oauth account as requiring re-consent (Microsoft invalid_grant).

        ADR-0025 §3 step 5: leave ``is_active`` untouched; the worker skips
        sync while ``oauth_needs_consent`` is true and the UI shows "reconnect".
        """
        await self._s.execute(
            update(MailAccount)
            .where(MailAccount.id == account_id)
            .values(oauth_needs_consent=True, updated_at=datetime.now(UTC))
        )

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

    async def mark_transient_error(self, account_id: int, *, error: str) -> None:
        """Record a TRANSIENT sync error (ADR-0026 §2).

        Writes ``last_sync_error`` only. Deliberately does NOT touch
        ``consecutive_failures`` (transient must not count toward auto-disable),
        ``is_active`` (transient must not disable the account), or
        ``last_synced_at`` (its semantics = "time of last *successful* sync";
        leaving it untouched keeps the account near the head of the
        ``list_active()`` ORDER BY so it retries promptly without starving
        healthy mailboxes — see ADR-0026 §2 starvation invariant).
        """
        await self._s.execute(
            update(MailAccount)
            .where(MailAccount.id == account_id)
            .values(
                last_sync_error=error[:500],
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
