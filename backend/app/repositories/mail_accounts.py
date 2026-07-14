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

#: ``last_sync_error`` written when Microsoft invalidates the refresh token and
#: the mailbox is flagged ``oauth_needs_consent`` (ADR-0046 §3 H7). Same
#: ``"<prefix>: <detail>"`` shape as the worker's sync errors; mirrored to the
#: CRM so the box stops reading as "green and healthy" while it is not syncing.
OAUTH_NEEDS_CONSENT_SYNC_ERROR = "oauth_needs_consent: требуется переподключение Outlook"


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

    # ADR-0044 §3 (lock-step): every reader of ``mail_accounts.group_id`` is
    # removed BEFORE the DROP COLUMN (phase C) — ``list_for_group_or_owner`` /
    # ``get_for_group_or_owner`` / ``list_account_ids_visible`` /
    # ``list_account_ids_in_group`` / ``attach_orphans_to_group`` /
    # ``update_group`` served the team-based visibility the connector no longer
    # has (its single owner is the ``crm-service`` super_admin).

    async def list_canonical_account_ids(self) -> list[int]:
        """One canonical ``mail_accounts.id`` per ``LOWER(email)`` (round-18).

        Used by super-admin "all teams" views to hide duplicate IMAP polls
        when two teams independently added the same mailbox. We keep the
        oldest row (``MIN(id)``) as canonical — deterministic and stable
        across requests.
        """
        stmt = select(func.min(MailAccount.id)).group_by(func.lower(MailAccount.email))
        return [int(r[0]) for r in (await self._s.execute(stmt)).all()]

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

    async def insert_oauth_account_with_id(
        self,
        *,
        account_id: int,
        user_id: int,
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

        ADR-0046 §3 (H7): additionally write ``last_sync_error`` in the SAME
        transaction. Without it the mailbox mirrors to the CRM as
        ``is_active=true / consecutive_failures=0 / last_sync_error=NULL`` — a
        green dot on a box that is not syncing at all (the worker clean-skips
        it every cycle). ``is_active`` / ``consecutive_failures`` stay untouched
        so the ADR-0025 §3 step 5 invariant holds (needs-consent never disables
        the mailbox and never feeds auto-disable). The call-site enqueues the
        CRM status event after COMMIT.
        """
        await self._s.execute(
            update(MailAccount)
            .where(MailAccount.id == account_id)
            .values(
                oauth_needs_consent=True,
                last_sync_error=OAUTH_NEEDS_CONSENT_SYNC_ERROR,
                updated_at=datetime.now(UTC),
            )
        )

    async def mark_oauth_needs_consent_error(self, account_id: int) -> bool:
        """Guarded write of the needs-consent marker into ``last_sync_error`` (ADR-0046 §3 H7b).

        Single ``UPDATE ... WHERE last_sync_error IS DISTINCT FROM <marker>`` (no
        preceding SELECT — no race window). Returns ``True`` only when a row was
        actually updated; the caller then (and only then) enqueues the CRM status
        event after COMMIT. A mailbox that already carries the marker is a no-op:
        no write, no push — otherwise every sync interval would emit a status
        event for every dead mailbox.

        Used by the worker's clean-skip branch, which short-circuits BEFORE the
        token refresh and therefore never reaches the transition point (H7a).
        ``is_active`` / ``consecutive_failures`` / ``last_synced_at`` are NOT
        touched (ADR-0025 §3 step 5 + ADR-0046 §1).
        """
        stmt = (
            update(MailAccount)
            .where(
                MailAccount.id == account_id,
                MailAccount.last_sync_error.is_distinct_from(OAUTH_NEEDS_CONSENT_SYNC_ERROR),
            )
            .values(
                last_sync_error=OAUTH_NEEDS_CONSENT_SYNC_ERROR,
                updated_at=datetime.now(UTC),
            )
            .returning(MailAccount.id)
        )
        row = (await self._s.execute(stmt)).one_or_none()
        return row is not None

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

    async def disable_and_stamp_alert(self, account_id: int) -> bool:
        """Auto-disable a mailbox + stamp the alert idempotency marker (ADR-0033 §2).

        Combined guarded UPDATE (matches the ADR §2 SQL): sets
        ``is_active=false`` AND ``disabled_alert_sent_at=now()`` only when the
        stamp was still ``NULL``. Returns ``True`` when the stamp transitioned
        ``NULL → now()`` (a clean Active→Disabled transition — the caller must
        enqueue exactly one Telegram alert), ``False`` when a row was already
        stamped (theoretical two-cycle race — no second alert).

        Called exclusively from ``worker.sync_cycle._disable_after_failures``
        inside the same transaction as the ``account_auto_disabled`` audit row.
        The stamp is written regardless of ``MAILBOX_DOWN_ALERT_ENABLED`` (the
        enqueue is gated by that flag, not the stamp) so toggling the feature on
        later never re-alerts a mailbox disabled while it was off.
        """
        now = datetime.now(UTC)
        stmt = (
            update(MailAccount)
            .where(
                MailAccount.id == account_id,
                MailAccount.disabled_alert_sent_at.is_(None),
            )
            .values(
                is_active=False,
                disabled_alert_sent_at=now,
                updated_at=now,
            )
            .returning(MailAccount.id)
        )
        row = (await self._s.execute(stmt)).one_or_none()
        return row is not None

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

        Deliberately does NOT touch ``last_synced_at``: ADR-0046 §1 makes its
        semantics normative and single-valued — "time of the last SUCCESSFUL
        sync", written only by :meth:`mark_sync_success`. Bumping it on a
        PERMANENT failure used to make a broken mailbox look freshly synced,
        which (a) silently suppressed the next TRANSIENT error via
        ``_should_suppress_transient`` (its window measures the age of the last
        *success*) and (b) contradicted the CRM contract/UI ("last successful
        sync"). Freezing it keeps the failing box at the head of the
        ``list_active()`` ORDER BY — no starvation (the cycle has no LIMIT).
        """
        values: dict[str, object] = {
            "consecutive_failures": MailAccount.consecutive_failures + 1,
            "last_sync_error": error[:500],
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
