"""Worker sync cycle (ADR-0008 + ADR-0013).

Two scheduled jobs (see ``worker/app/main.py``):

* ``sync_cycle`` — every ``SYNC_INTERVAL_MINUTES`` (default 5).
  Reads ALL active mail accounts and runs :func:`sync_one_account` for
  each under an :class:`asyncio.Semaphore` of size ``MAX_CONCURRENT_IMAP``.
* ``force_sync_dispatch`` — every 10 seconds.
  Drains the ``force_sync:{account_id}`` Redis markers (set by the API
  endpoint ``POST /accounts/{id}/sync``) and syncs ONLY those accounts,
  giving users a sub-10-second feedback loop on the "Sync now" button
  without driving the regular polling cadence below 5 minutes.

The two jobs share :func:`_run_for_accounts` for the actual per-account
fan-out so semaphore semantics, error handling and logging stay
identical.

Idempotency: ``ON CONFLICT DO NOTHING`` on
``(mail_account_id, uidvalidity, uid)``.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as _dt
import json
import uuid
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Literal, cast

import structlog
from cryptography.exceptions import InvalidTag

# NOTE: worker imports from ``backend.app.*`` (repositories + audit) — this
# coupling is intentional and accepted by reviewers per the rework round 2
# decision: keep `repositories/` in `backend/` to avoid moving 6 files
# without architect sign-off. Both containers ship `backend/` + `worker/` +
# `shared/` per ``deploy/Dockerfile``.
from backend.app.audit import AuditWriter
from backend.app.exceptions import InvalidHostError
from backend.app.oauth.service import OAuthError, OAuthRefreshInvalidError, OutlookTokenService
from backend.app.repositories.mail_accounts import MailAccountsRepo
from backend.app.repositories.messages import MessagesRepo
from backend.app.repositories.users import UsersRepo
from backend.app.security import assert_public_host
from backend.app.tags.service import TagsService
from backend.app.telegram.notify_service import TelegramNotifyService
from shared.config import get_settings
from shared.crypto import decrypt_mail_password
from shared.db import make_session
from shared.logging import get_logger
from shared.models import MailAccount
from shared.redis_client import get_redis
from shared.storage import get_storage
from worker.app.error_classify import classify, error_prefix, is_explicit_permanent
from worker.app.imap_fetcher import FetchedBox, FetchedMessage, fetch_blocking
from worker.app.mailbox_alert_dispatch import MAILBOX_ALERT_QUEUE_KEY
from worker.app.push_notify_dispatch import _QUEUE_KEY as _PUSH_NOTIFY_QUEUE_KEY

log = get_logger(__name__)

# ADR-0026: outcome of one account's sync, used by the two-phase
# ``_run_for_accounts`` to apply bump/disable AFTER the circuit-breaker
# decision (so a mass infra outage cannot disable everything at once).
AccountSyncOutcome = Literal["ok", "transient", "permanent"]

# ADR-0034 §3.2: our own outbound forwards stamp ``X-Forwarded-By:
# mail-aggregator``. A newly-fetched message already carrying this stamp is a
# copy of one we forwarded (it landed back in one of our mailboxes) and must
# NOT be re-enqueued for forwarding — loop-guard part 1.
_FORWARD_STAMP = "mail-aggregator"


def _carries_own_forward_stamp(fmsg: FetchedMessage) -> bool:
    """True when the message already carries our ``X-Forwarded-By`` stamp."""
    value = fmsg.x_forwarded_by
    return value is not None and _FORWARD_STAMP in value.lower()


@dataclass(slots=True)
class _AccountResult:
    """Per-account result carried out of :func:`sync_one_account` so phase 2 of
    :func:`_run_for_accounts` can apply the circuit-breaker decision."""

    account_id: int
    user_id: int
    new_count: int
    conflict_count: int
    outcome: AccountSyncOutcome
    # Only set for ``outcome == "permanent"``: the error text already written
    # to ``last_sync_error`` (phase 0), the UI prefix (for the audit reason on
    # an explicit-permanent disable) and the "instant disable" flag (explicit
    # auth/decrypt — no threshold needed, ADR-0026 §2/§3).
    error: str | None = None
    prefix: str | None = None
    explicit_permanent: bool = False


@dataclass(slots=True)
class _TagInputMessage:
    """Minimal message-shaped tuple passed to ``TagsService.apply_tags_to_message``.

    The service expects a ``Message``-shaped object with ``id``, ``subject``,
    ``body_text``, ``body_html``, ``from_addr`` and ``from_name``.
    Constructing a real ORM ``Message`` here would require extra round-trips
    (we already have the values from ``FetchedMessage`` + ``inserted_id``); a
    tiny dataclass keeps the call clean and avoids a SELECT round-trip.

    round-29 (ADR-0017 §4.3): ``body_html`` is carried so the worker hook can
    match ``body_contains`` against the tag-stripped HTML body the UI renders
    (Apple ships different text in text/plain vs text/html). It is the same
    raw HTML written to ``messages.body_html`` by ``insert_message_idempotent``.
    """

    id: int
    subject: str | None
    body_text: str
    body_html: str | None
    from_addr: str
    from_name: str | None


# ---------------------------------------------------------------------------
# Forced sync queue (Redis ``force_sync:{account_id}`` markers)
# ---------------------------------------------------------------------------


async def _drain_forced_account_ids() -> set[int]:
    """Return account_ids that were marked for force-sync, deleting markers."""
    out: set[int] = set()
    redis = get_redis()
    async for raw_key in redis.scan_iter(match="force_sync:*", count=500):
        key = raw_key if isinstance(raw_key, str) else raw_key.decode()
        suffix = key.split(":", 1)[1]
        try:
            out.add(int(suffix))
        except ValueError:
            continue
        await redis.delete(key)
    return out


# ---------------------------------------------------------------------------
# Per-account sync
# ---------------------------------------------------------------------------


async def sync_one_account(
    account: MailAccount,
    *,
    timeout_seconds: int,
    initial_sync_days: int,
    max_body_bytes: int,
    max_att_bytes: int,
) -> _AccountResult:
    """Sync one account. Returns an :class:`_AccountResult`.

    ADR-0026 §2/§3: this function NEVER bumps ``consecutive_failures`` or
    disables the account in the moment of error. On any error it (phase 0)
    classifies via :func:`error_classify.classify` / :func:`error_prefix`,
    writes ``last_sync_error`` IMMEDIATELY (transient via the no-bump repo
    method; permanent writes the error too but defers bump/disable), and
    returns the ``outcome`` so :func:`_run_for_accounts` can apply
    bump/disable after the circuit-breaker decision.

    Side-effect: updates ``mail_accounts`` row + writes any new ``messages``
    + ``attachments`` + uploads attachment blobs to MinIO.
    """
    storage = get_storage()
    # ADR-0022 §2.1 (round-31): TG_NOTIFY_ALL_MESSAGES gates whether every
    # inserted message is enqueued for Telegram notification or only tagged
    # ones. lru-cached; one read per account-sync is cheap.
    settings = get_settings()
    cycle_log = log.bind(mail_account_id=account.id, user_id=account.user_id)
    cycle_log.info("sync_account_start")

    # ADR-0025 §4: resolve credentials — an XOAUTH2 access token for
    # oauth_outlook accounts (refreshed if needed) or the decrypted password
    # otherwise. ``None`` means "skip this account": either a clean skip
    # (needs-consent) or a classified failure already recorded in phase 0.
    creds_result = await _resolve_credentials(account, cycle_log)
    if isinstance(creds_result, _AccountResult):
        return creds_result
    if creds_result is None:
        return _AccountResult(
            account_id=account.id,
            user_id=account.user_id,
            new_count=0,
            conflict_count=0,
            outcome="ok",
        )
    password, access_token = creds_result

    # SSRF guard per ``docs/06-security.md`` sec. 4: backend (test) AND worker
    # (sync) must verify the host doesn't resolve to a private network. Guards
    # against DNS-rebinding and tampered DB rows pointing at internal hosts.
    # No-op in dev (so localhost mock IMAP servers still work).
    #
    # ADR-0026 root-cause B fix: when DNS is down, ``assert_public_host`` raises
    # ``InvalidHostError("Could not resolve host", ...)``. That used to disable
    # the account immediately. Now it is classified like any other error — the
    # "could not resolve" substring makes it TRANSIENT (no disable, retries).
    try:
        assert_public_host(account.imap_host, port=account.imap_port)
    except InvalidHostError as exc:
        detail = str(exc.message) if hasattr(exc, "message") else str(exc)
        return await _handle_sync_error(account, exc, detail=detail, cycle_log=cycle_log)

    try:
        box: FetchedBox = await asyncio.wait_for(
            asyncio.to_thread(
                fetch_blocking,
                host=account.imap_host,
                port=account.imap_port,
                ssl_on=account.imap_ssl,
                username=account.email,
                password=password,
                access_token=access_token,
                last_synced_uidnext=account.last_synced_uidnext,
                last_uidvalidity=account.last_uidvalidity,
                initial_sync_days=initial_sync_days,
                max_body_bytes=max_body_bytes,
                max_att_bytes=max_att_bytes,
                timeout=timeout_seconds,
            ),
            timeout=timeout_seconds,
        )
    except Exception as exc:
        text = str(exc).replace("\r", " ").replace("\n", " ")
        return await _handle_sync_error(account, exc, detail=text, cycle_log=cycle_log)

    # Save messages + attachments.
    new_count = 0
    conflict_count = 0
    tags_applied_total = 0
    # ADR-0022 §2.1: collect message_ids that received at least one tag so
    # we can LPUSH them onto ``tg_notify_queue`` after the transaction
    # commits. Inserting from inside the transaction would risk pushing
    # message_ids whose tags get rolled back on tag-apply failure.
    notified_message_ids: list[int] = []
    # ADR-0034 §3.2: separate accumulator for forwarding (NOT tag-gated — we
    # forward ALL new incoming of a team). Only mailboxes bound to a team
    # (group_id NOT NULL) and only messages that do not already carry our
    # forward stamp (loop-guard part 1) are collected.
    forward_ids: list[int] = []
    async with make_session() as s, s.begin():
        repo = MessagesRepo(s)
        tags_service = TagsService(s)
        for fmsg in box.new_messages:
            inserted_id = await repo.insert_message_idempotent(
                mail_account_id=account.id,
                uid=fmsg.uid,
                uidvalidity=box.uidvalidity,
                message_id_header=fmsg.message_id_header,
                from_addr=fmsg.from_addr,
                from_name=fmsg.from_name,
                to_addrs=fmsg.to_addrs,
                cc_addrs=fmsg.cc_addrs,
                subject=fmsg.subject,
                internal_date=fmsg.internal_date,
                body_text=fmsg.body_text,
                body_html=fmsg.body_html,
                body_truncated=fmsg.body_truncated,
                body_present=fmsg.body_present,
                in_reply_to=fmsg.in_reply_to,
                refs_header=fmsg.refs_header,
            )
            if inserted_id is None:
                conflict_count += 1
                continue
            new_count += 1
            # ADR-0034 §3.2: collect for forwarding — team mailbox only,
            # loop-guard part 1 (skip our own already-forwarded copies).
            if account.group_id is not None and not _carries_own_forward_stamp(fmsg):
                forward_ids.append(inserted_id)
            for fatt in fmsg.attachments:
                att_id = await repo.reserve_attachment_id()
                key = storage.build_key(
                    user_id=account.user_id,
                    mail_account_id=account.id,
                    message_uid=fmsg.uid,
                    attachment_id=att_id,
                    filename=fatt.filename,
                )
                skipped = fatt.size_bytes > max_att_bytes
                if not skipped and fatt.payload:
                    try:
                        await storage.put_object(key, fatt.payload, fatt.content_type)
                    except Exception as exc:
                        # If MinIO write fails, mark as skipped so the row
                        # in DB stays consistent. Log the error.
                        cycle_log.warning(
                            "sync_account_attachment_put_fail",
                            detail=str(exc)[:200],
                        )
                        skipped = True
                await repo.insert_attachment_with_id(
                    attachment_id=att_id,
                    message_id=inserted_id,
                    filename=fatt.filename,
                    content_type=fatt.content_type,
                    size_bytes=fatt.size_bytes,
                    s3_key=key,
                    skipped_too_large=skipped,
                )

            # Apply tags (ADR-0017 §5). Best-effort within the same
            # transaction: a SQL fault here would abort all messages in
            # this batch, so we catch broadly and log a warning. The
            # ``ON CONFLICT DO NOTHING`` in the underlying SQL keeps it
            # idempotent against retries — no tag duplication risk.
            try:
                applied = await tags_service.apply_tags_to_message(
                    message=_TagInputMessage(
                        id=inserted_id,
                        subject=fmsg.subject,
                        body_text=fmsg.body_text,
                        body_html=fmsg.body_html,
                        from_addr=fmsg.from_addr,
                        from_name=fmsg.from_name,
                    ),
                    mail_account_id=account.id,
                )
                tags_applied_total += applied
                # ADR-0022 §2.1 (round-31): enqueue EVERY inserted message
                # when TG_NOTIFY_ALL_MESSAGES is on (default); otherwise keep
                # the historical "only tagged" behaviour (applied > 0).
                if settings.TG_NOTIFY_ALL_MESSAGES or applied > 0:
                    notified_message_ids.append(inserted_id)
            except Exception as exc:
                # Don't lose the message; just record that we couldn't
                # tag it. Subsequent ingest of the same UID is impossible
                # (UNIQUE constraint), so unlike the message itself the
                # tag application has no automatic retry path. That's
                # acceptable — operators can re-run apply-to-existing
                # from /tags/{id}/edit if a rule was misconfigured.
                cycle_log.warning(
                    "apply_tags_failed",
                    message_id=inserted_id,
                    detail=str(exc)[:200],
                )

    # Mark sync success.
    async with make_session() as s, s.begin():
        await MailAccountsRepo(s).mark_sync_success(
            account.id,
            last_synced_uidnext=box.uidnext,
            last_uidvalidity=box.uidvalidity,
        )

    # ADR-0022 §2.1 (round-31): enqueue Telegram-notifications for the
    # collected message_ids — every inserted message when
    # TG_NOTIFY_ALL_MESSAGES is on (default), else only those that got at
    # least one tag. LPUSH after COMMIT so a recipient SQL inside the
    # dispatcher can see the committed rows. We swallow any failure here
    # — a Redis outage must NEVER abort the sync cycle.
    if notified_message_ids:
        try:
            async with make_session() as s:
                pushed = await TelegramNotifyService(s).enqueue_message_ids(notified_message_ids)
            cycle_log.info(
                "tg_notify_enqueued",
                count=pushed,
                mail_account_id=account.id,
            )
        except Exception as exc:
            cycle_log.warning(
                "tg_notify_enqueue_failed",
                detail=str(exc)[:200],
                count=len(notified_message_ids),
            )

        # ADR-0023 §3.1: enqueue outbound-webhook deliveries for the same
        # message_ids. Independent try/except — a webhook-enqueue failure
        # must not affect the TG channel and vice versa.
        try:
            from backend.app.webhooks.dispatch_service import WebhookDispatchService

            async with make_session() as s:
                pushed = await WebhookDispatchService(s).enqueue_message_ids(notified_message_ids)
            cycle_log.info(
                "webhook_enqueued",
                count=pushed,
                mail_account_id=account.id,
            )
        except Exception as exc:
            cycle_log.warning(
                "webhook_enqueue_failed",
                detail=str(exc)[:200],
                count=len(notified_message_ids),
            )

        # ADR-0027 §3.1: enqueue push-only per-team bot deliveries for the
        # same message_ids onto the separate ``push_notify_queue``. Third
        # independent try/except — a push-enqueue failure must not affect the
        # main TG channel or webhooks (and vice versa). The actual bot is
        # selected later (by account.group_id) in ``push_notify_dispatch``.
        if settings.push_team_bots_enabled:
            try:
                from backend.app.telegram.notify_service import _QueuePayload

                redis = get_redis()
                items = [
                    _QueuePayload(message_id=int(mid), source="sync").to_json()
                    for mid in notified_message_ids
                ]
                await cast(Awaitable[int], redis.lpush(_PUSH_NOTIFY_QUEUE_KEY, *items))
                cycle_log.info(
                    "push_notify_enqueued",
                    count=len(items),
                    mail_account_id=account.id,
                )
            except Exception as exc:
                cycle_log.warning(
                    "push_notify_enqueue_error",
                    detail=str(exc)[:200],
                    count=len(notified_message_ids),
                )

    # ADR-0034 §3.2: enqueue forward deliveries. Independent of the TG/webhook
    # channels — separate accumulator (all new team incoming, minus loop-guarded
    # ones) and its own try/except so a Redis failure here never aborts the sync
    # cycle nor the other notification channels. Consumer does the final config /
    # temporal / loop / dedup checks (worker.forward_dispatch, §14.4).
    if forward_ids:
        try:
            from backend.app.forwarding.dispatch_service import ForwardDispatchService

            async with make_session() as s:
                pushed = await ForwardDispatchService(s).enqueue_message_ids(forward_ids)
            cycle_log.info(
                "forward_enqueued",
                count=pushed,
                mail_account_id=account.id,
            )
        except Exception as exc:
            cycle_log.warning(
                "forward_enqueue_failed",
                detail=str(exc)[:200],
                count=len(forward_ids),
            )

    cycle_log.info(
        "sync_account_finish",
        new_messages=new_count,
        conflicts=conflict_count,
        tags_applied=tags_applied_total,
    )
    return _AccountResult(
        account_id=account.id,
        user_id=account.user_id,
        new_count=new_count,
        conflict_count=conflict_count,
        outcome="ok",
    )


# ---------------------------------------------------------------------------
# Helpers used by sync_one_account
# ---------------------------------------------------------------------------


def _classifier_auth_type(account: MailAccount) -> str | None:
    """Return the ``auth_type`` to hand to the error classifier (ADR-0028).

    Normally the account's own ``auth_type``. The
    ``SYNC_OAUTH_LOGIN_FAILED_TRANSIENT`` kill-switch (default True) gates the
    OAuth rule 7b: when it is False we pass ``None`` for ``oauth_outlook``
    accounts so the classifier falls back to the legacy permanent path for an
    IMAP "login failed" (no code redeploy needed to revert). Password accounts
    are unaffected — their ``auth_type`` never triggers rule 7b anyway.
    """
    if (
        account.auth_type == "oauth_outlook"
        and not get_settings().SYNC_OAUTH_LOGIN_FAILED_TRANSIENT
    ):
        return None
    return account.auth_type


async def _handle_sync_error(
    account: MailAccount,
    exc: BaseException,
    *,
    detail: str,
    cycle_log: structlog.stdlib.BoundLogger,
) -> _AccountResult:
    """Phase 0 of ADR-0026: classify, write ``last_sync_error`` immediately,
    and build the per-account result. Bump/disable are deferred to phase 2.

    For TRANSIENT (incl. DNS/network/rate-limit and the fail-open rule 10) we
    write ``last_sync_error`` via the no-bump repo method — the account is NOT
    disabled and the counter is untouched. For PERMANENT we write
    ``last_sync_error`` too (so the operator always sees the cause even when the
    breaker later suppresses the disable), but the bump/disable itself happens in
    phase 2 under the circuit-breaker decision.

    ADR-0028: the account's ``auth_type`` is forwarded to the classifier so an
    IMAP "login failed" on an ``oauth_outlook`` account is treated as a
    transient Microsoft flake (rule 7b), not a permanent instant-disable. The
    ``SYNC_OAUTH_LOGIN_FAILED_TRANSIENT`` kill-switch reverts to the legacy
    behaviour by passing ``auth_type=None`` (the classifier stays pure).
    """
    auth_type = _classifier_auth_type(account)
    cls = classify(exc, auth_type=auth_type)
    prefix = error_prefix(exc, auth_type=auth_type)
    detail_clean = detail.replace("\r", " ").replace("\n", " ")[:200]
    error_text = f"{prefix}: {detail_clean}"

    if cls == "transient":
        # ADR-0026 update §2: a sporadic transient flake on an otherwise-working
        # mailbox must not surface in the UI. If the last SUCCESSFUL sync was
        # within SYNC_TRANSIENT_SUPPRESS_MINUTES we suppress the
        # ``last_sync_error`` write (the next cycle retries / succeeds). If the
        # last success is stale (or never happened) we DO write it — the sync is
        # genuinely stuck and the operator must see it. The consecutive-failures
        # counter is never touched for transient either way.
        suppress = _should_suppress_transient(account.last_synced_at)
        # Rule 10 (unrecognised, incl. programming errors) -> ERROR + traceback
        # for alerting; recognised network/IMAP/OAuth transients -> WARNING.
        if prefix == "error":
            cycle_log.error(
                "sync_account_unexpected_error",
                exc_info=True,
                detail=error_text,
            )
        else:
            cycle_log.warning(
                "sync_account_transient",
                error_class=cls,
                prefix=prefix,
                detail=error_text,
                last_sync_error_suppressed=suppress,
            )
        if not suppress:
            await _record_transient(account.id, error=error_text)
        return _AccountResult(
            account_id=account.id,
            user_id=account.user_id,
            new_count=0,
            conflict_count=0,
            outcome="transient",
        )

    # PERMANENT: write last_sync_error now (phase 0); defer bump/disable.
    cycle_log.warning(
        "sync_account_permanent",
        error_class=cls,
        prefix=prefix,
        detail=error_text,
    )
    await _record_transient(account.id, error=error_text)
    return _AccountResult(
        account_id=account.id,
        user_id=account.user_id,
        new_count=0,
        conflict_count=0,
        outcome="permanent",
        error=error_text,
        prefix=prefix,
        explicit_permanent=is_explicit_permanent(exc, auth_type=auth_type),
    )


async def _resolve_credentials(
    account: MailAccount, cycle_log: structlog.stdlib.BoundLogger
) -> tuple[str | None, str | None] | _AccountResult | None:
    """Resolve ``(password, access_token)`` for the account.

    Returns:
    - ``tuple`` on success (exactly one of password / access_token set);
    - ``None`` for a clean skip (oauth needs-consent — no failure recorded);
    - :class:`_AccountResult` when a classified failure occurred (the helper
      already wrote ``last_sync_error`` in phase 0; bump/disable deferred).
    """
    if account.auth_type == "oauth_outlook":
        if account.oauth_needs_consent:
            # Refresh invalidated by Microsoft — skip without bumping the
            # failure counter (ADR-0025 §3 step 5); UI prompts re-consent.
            cycle_log.info("sync_account_oauth_needs_consent")
            return None
        return await _resolve_oauth_access_token(account, cycle_log)

    try:
        assert account.encrypted_password is not None
        password = decrypt_mail_password(account.encrypted_password, account.id)
    except (InvalidTag, AssertionError) as exc:
        # Rule 9 — decrypt failure -> PERMANENT (explicit, instant disable).
        return await _handle_sync_error(account, exc, detail="decrypt_fail", cycle_log=cycle_log)
    return password, None


async def _resolve_oauth_access_token(
    account: MailAccount, cycle_log: structlog.stdlib.BoundLogger
) -> tuple[str | None, str | None] | _AccountResult | None:
    """Get a valid XOAUTH2 access token for an oauth_outlook account (ADR-0025 §3).

    Returns:
    - ``(None, token)`` on success;
    - ``None`` for ``invalid_grant`` (already marked needs-consent — clean skip);
    - :class:`_AccountResult` for any other token error (classified, phase 0).
    """
    try:
        async with make_session() as s:
            token = await OutlookTokenService(s).get_valid_access_token(account)
        return None, token
    except OAuthRefreshInvalidError:
        # Already marked needs-consent inside the service; nothing else to do.
        cycle_log.info("sync_account_oauth_refresh_invalidated")
        return None
    except OAuthError as exc:
        # OAuth 5xx/429/network -> transient (rule 7); invalid_grant handled
        # above via OAuthRefreshInvalidError. The classifier reads the code.
        return await _handle_sync_error(
            account, exc, detail=f"oauth_token_error: {exc.code}", cycle_log=cycle_log
        )
    except Exception as exc:  # network / unexpected — classify (fail-open transient)
        return await _handle_sync_error(
            account,
            exc,
            detail=f"oauth_token_unexpected: {type(exc).__name__}",
            cycle_log=cycle_log,
        )


def _should_suppress_transient(last_synced_at: _dt.datetime | None) -> bool:
    """True if a TRANSIENT ``last_sync_error`` write should be suppressed.

    ADR-0026 update §2: a sporadic flake on a mailbox that synced successfully
    within ``SYNC_TRANSIENT_SUPPRESS_MINUTES`` is hidden from the UI (the next
    cycle retries / succeeds). A stale (older than the window) or ``NULL``
    ``last_synced_at`` means the sync is genuinely stuck -> do NOT suppress (the
    operator must see the error).

    ``SYNC_TRANSIENT_SUPPRESS_MINUTES == 0`` disables suppression entirely
    (every transient error is written, the pre-update behaviour).
    """
    window_minutes = get_settings().SYNC_TRANSIENT_SUPPRESS_MINUTES
    if window_minutes <= 0 or last_synced_at is None:
        return False
    # ``last_synced_at`` is stored timezone-aware (DateTime(timezone=True));
    # coerce a naive value defensively so the subtraction never raises.
    if last_synced_at.tzinfo is None:
        last_synced_at = last_synced_at.replace(tzinfo=_dt.UTC)
    age = _dt.datetime.now(_dt.UTC) - last_synced_at
    return age <= _dt.timedelta(minutes=window_minutes)


async def _record_transient(account_id: int, *, error: str) -> None:
    """Write ``last_sync_error`` without bumping the counter (ADR-0026 §2)."""
    async with make_session() as s, s.begin():
        await MailAccountsRepo(s).mark_transient_error(account_id, error=error)


async def _record_failure(account_id: int, *, error: str, disable: bool) -> int:
    """Bump ``consecutive_failures`` (+ optional disable). Returns new count.

    Used in phase 2 of :func:`_run_for_accounts` for PERMANENT accounts when the
    circuit-breaker did NOT trip.
    """
    async with make_session() as s, s.begin():
        return await MailAccountsRepo(s).mark_sync_failure(account_id, error=error, disable=disable)


async def _enqueue_mailbox_alert(account_id: int, *, reason: str) -> None:
    """LPUSH one mailbox-down alert onto ``mailbox_alert_queue`` (ADR-0033 §4).

    Called AFTER the disable transaction commits, only on a clean
    ``NULL → now()`` stamp transition and only when
    ``MAILBOX_DOWN_ALERT_ENABLED`` is on. Wrapped in ``try/except`` with a log —
    a Redis outage must NEVER abort the sync cycle (same isolation as the
    ``tg_notify`` enqueue). The stamp is already committed, so a lost enqueue is
    not retried (fire-and-forget, TD-042).
    """
    try:
        redis = get_redis()
        payload = json.dumps(
            {"v": 1, "mail_account_id": account_id, "reason": reason},
            separators=(",", ":"),
        )
        await cast(Awaitable[int], redis.lpush(MAILBOX_ALERT_QUEUE_KEY, payload))
        log.info("mailbox_alert_enqueued", mail_account_id=account_id, reason=reason)
    except Exception as exc:
        log.warning(
            "mailbox_alert_enqueue_failed",
            mail_account_id=account_id,
            detail=str(exc)[:200],
        )


async def _disable_after_failures(account_id: int, *, user_id: int, reason: str) -> None:
    """Disable the account and write an ``account_auto_disabled`` audit row.

    ``reason`` is a stable string for ``details.reason``:
    ``"N_consecutive_failures"`` (threshold) or ``"auth_failed"`` /
    ``"decrypt_fail"`` (explicit permanent, instant disable). ADR-0026 §3.

    ADR-0033: this is the ONLY worker auto-disable point and thus the only
    mailbox-down alert trigger. In the same transaction as ``is_active=false`` +
    audit, a guarded UPDATE stamps ``disabled_alert_sent_at=now()`` iff it was
    ``NULL`` (idempotency "one alert per transition"). After COMMIT, if the
    stamp really transitioned (``NULL → now()``) AND the feature is enabled, we
    enqueue exactly one alert. Circuit-breaker suppression / transient / OAuth
    needs-consent / manual disable never reach this function, so they never
    alert.
    """
    async with make_session() as s, s.begin():
        stamped = await MailAccountsRepo(s).disable_and_stamp_alert(account_id)
        # The audit log requires ``actor_user_id``. For system actions we
        # attribute to the super-admin (the one with role='super_admin').
        # If for whatever reason there is no admin row (e.g. seed didn't
        # run), we fall back to the affected user.
        admin = await UsersRepo(s).get_admin()
        actor_id = admin.id if admin else user_id
        await AuditWriter(s).log(
            actor_user_id=actor_id,
            action="account_auto_disabled",
            target_user_id=user_id,
            details={
                "mail_account_id": account_id,
                "reason": reason,
            },
        )

    # After COMMIT: enqueue exactly one alert on the clean NULL → now()
    # transition. The stamp is written regardless of the flag; only the enqueue
    # is gated by ``MAILBOX_DOWN_ALERT_ENABLED`` (ADR-0033 §8).
    if stamped and get_settings().MAILBOX_DOWN_ALERT_ENABLED:
        await _enqueue_mailbox_alert(account_id, reason=reason)


async def _audit_mass_failure_suppressed(
    *,
    total: int,
    permanent_failures: int,
    transient_failures: int,
    ratio: float,
    threshold_ratio: float,
    threshold_min: int,
    fallback_user_id: int,
) -> None:
    """Write the once-per-cycle ``sync_mass_failure_suppressed`` audit row when
    the circuit-breaker trips (ADR-0026 §3). ``actor_user_id`` is the system
    super-admin (falls back to an affected user if no admin row exists)."""
    async with make_session() as s, s.begin():
        admin = await UsersRepo(s).get_admin()
        actor_id = admin.id if admin else fallback_user_id
        await AuditWriter(s).log(
            actor_user_id=actor_id,
            action="sync_mass_failure_suppressed",
            details={
                "total": total,
                "permanent_failures": permanent_failures,
                "transient_failures": transient_failures,
                "ratio": round(ratio, 4),
                "threshold_ratio": threshold_ratio,
                "threshold_min": threshold_min,
            },
        )


# ---------------------------------------------------------------------------
# Cycle / dispatcher
# ---------------------------------------------------------------------------


async def _run_for_accounts(accounts: list[MailAccount]) -> tuple[int, int, int]:
    """Run :func:`sync_one_account` for each account under the IMAP semaphore.

    Two-phase per ADR-0026 §3:

    * Phase 1 — ``asyncio.gather`` all accounts (``return_exceptions=True`` so a
      single failure never aborts the others, per ``docs/05-modules.md`` §14).
      Each ``sync_one_account`` already wrote ``last_sync_error`` in phase 0;
      here we only collect outcomes and compute ``total`` / ``permanent_count``.
    * Phase 2 — circuit-breaker decision. If ``breaker_tripped`` we suppress
      BOTH the bump and the disable for every permanent account this cycle
      (probable common infra outage — ``last_sync_error`` is already written so
      the operator still sees each cause). Otherwise we bump each permanent and
      disable on threshold OR explicit auth/decrypt.

    Returns ``(accounts_ok, accounts_failed, new_messages)`` where
    ``accounts_failed`` counts transient + permanent + unexpected-exception
    results (transient is a failure for the cycle stats even though it never
    disables).
    """
    if not accounts:
        return 0, 0, 0

    settings = get_settings()
    sem = asyncio.Semaphore(settings.MAX_CONCURRENT_IMAP)

    async def _bounded(acc: MailAccount) -> _AccountResult:
        async with sem:
            return await sync_one_account(
                acc,
                timeout_seconds=settings.IMAP_TIMEOUT_SECONDS,
                initial_sync_days=settings.INITIAL_SYNC_DAYS,
                max_body_bytes=settings.MAX_BODY_BYTES,
                max_att_bytes=settings.MAX_ATTACHMENT_BYTES,
            )

    raw_results = await asyncio.gather(*[_bounded(a) for a in accounts], return_exceptions=True)

    # --- Phase 1: collect outcomes ---------------------------------------
    results: list[_AccountResult] = []
    unexpected = 0
    new_msgs = 0
    for acc, res in zip(accounts, raw_results, strict=True):
        if isinstance(res, BaseException):
            # An exception escaping sync_one_account itself (not a per-account
            # sync error, which is already an _AccountResult). Defensive — log,
            # write last_sync_error as a transient (fail-open), count as failed,
            # but do NOT disable.
            log.error(
                "sync_account_runner_crashed",
                exc_info=res,
                mail_account_id=acc.id,
            )
            with contextlib.suppress(Exception):
                await _record_transient(acc.id, error=f"error: {type(res).__name__}: {res}"[:200])
            unexpected += 1
            continue
        results.append(res)
        new_msgs += res.new_count

    ok = sum(1 for r in results if r.outcome == "ok")
    permanent = [r for r in results if r.outcome == "permanent"]
    transient_count = sum(1 for r in results if r.outcome == "transient")
    permanent_count = len(permanent)
    total = len(results)
    failed = total - ok + unexpected

    # --- Phase 2: circuit-breaker decision -------------------------------
    ratio = (permanent_count / total) if total else 0.0
    breaker_tripped = (
        total >= settings.SYNC_MASS_FAILURE_MIN and ratio >= settings.SYNC_MASS_FAILURE_RATIO
    )

    if breaker_tripped:
        # Suppress BOTH bump and disable for all permanent accounts this cycle.
        # last_sync_error was already written in phase 0 (observability intact).
        log.warning(
            "sync_breaker_tripped",
            total=total,
            permanent_failures=permanent_count,
            transient_failures=transient_count,
            ratio=round(ratio, 4),
            threshold_ratio=settings.SYNC_MASS_FAILURE_RATIO,
            threshold_min=settings.SYNC_MASS_FAILURE_MIN,
        )
        if permanent:
            await _audit_mass_failure_suppressed(
                total=total,
                permanent_failures=permanent_count,
                transient_failures=transient_count,
                ratio=ratio,
                threshold_ratio=settings.SYNC_MASS_FAILURE_RATIO,
                threshold_min=settings.SYNC_MASS_FAILURE_MIN,
                fallback_user_id=permanent[0].user_id,
            )
    else:
        # Normal path: bump each permanent; disable on threshold or explicit
        # auth/decrypt.
        for r in permanent:
            new_failed = await _record_failure(
                r.account_id, error=r.error or "error", disable=False
            )
            if r.explicit_permanent:
                reason = r.prefix or "auth_failed"
                log.warning(
                    "sync_account_auto_disabled",
                    mail_account_id=r.account_id,
                    reason=reason,
                )
                await _disable_after_failures(r.account_id, user_id=r.user_id, reason=reason)
            elif new_failed >= settings.SYNC_MAX_CONSECUTIVE_FAILURES:
                log.warning(
                    "sync_account_auto_disabled",
                    mail_account_id=r.account_id,
                    consecutive_failures=new_failed,
                )
                await _disable_after_failures(
                    r.account_id,
                    user_id=r.user_id,
                    reason=f"{new_failed}_consecutive_failures",
                )

    return ok, failed, new_msgs


async def sync_cycle() -> None:
    """Run one full sync cycle. Designed to be invoked by APScheduler.

    NOTE: forced accounts (Redis ``force_sync:*`` markers) are handled by
    the dedicated :func:`force_sync_dispatch` job that ticks every 10
    seconds, so this cycle no longer drains the markers itself. By the
    time the 5-minute cycle runs, any pending forces have already been
    processed by the dispatcher (or will be, on its next tick — they are
    independent).
    """
    cycle_id = str(uuid.uuid4())
    structlog.contextvars.bind_contextvars(cycle_id=cycle_id)
    log.info("sync_cycle_start")

    try:
        async with make_session() as s:
            accounts = await MailAccountsRepo(s).list_active()

        ok, failed, new_msgs = await _run_for_accounts(accounts)

        log.info(
            "sync_cycle_finish",
            accounts_total=len(accounts),
            accounts_ok=ok,
            accounts_failed=failed,
            new_messages=new_msgs,
        )
    finally:
        structlog.contextvars.unbind_contextvars("cycle_id")


async def force_sync_dispatch() -> None:
    """Drain Redis ``force_sync:*`` markers and sync those accounts.

    Scheduled every 10 seconds (``worker/app/main.py``) to deliver
    sub-10-second latency on the "Sync now" UI button without lowering
    ``SYNC_INTERVAL_MINUTES`` (which would hammer every IMAP provider).

    Behaviour:

    * No markers in Redis -> return silently (no log spam).
    * Markers present but no MATCHING active account (e.g. account was
      disabled between marker write and dispatch) -> the markers are still
      removed by :func:`_drain_forced_account_ids`; we log a finish event
      with ``accounts_total=0`` so operators can see the dispatcher saw
      the markers.
    * APScheduler is configured ``max_instances=1, coalesce=True`` so two
      ticks cannot overlap.
    """
    forced_ids = await _drain_forced_account_ids()
    if not forced_ids:
        return

    cycle_id = str(uuid.uuid4())
    structlog.contextvars.bind_contextvars(cycle_id=cycle_id)
    log.info("force_sync_dispatch_start", account_ids_count=len(forced_ids))

    try:
        async with make_session() as s:
            accounts = await MailAccountsRepo(s).list_active_by_ids(sorted(forced_ids))

        ok, failed, new_msgs = await _run_for_accounts(accounts)

        log.info(
            "force_sync_dispatch_finish",
            accounts_requested=len(forced_ids),
            accounts_total=len(accounts),
            accounts_ok=ok,
            accounts_failed=failed,
            new_messages=new_msgs,
        )
    finally:
        structlog.contextvars.unbind_contextvars("cycle_id")
