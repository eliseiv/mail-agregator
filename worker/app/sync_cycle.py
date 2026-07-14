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

ADR-0044 §4 (phase A3): every hook of the decommissioned subsystems is removed
from the cycle — tag-apply, the ``tg_notify`` / ``webhook`` / ``push_notify`` /
``forward`` enqueues, the MinIO attachment download, the ``admin_audit`` writers
and the ``mailbox_alert`` queue. What stays: the message insert, the CRM
push-outbox (``crm_push``, ADR-0043 §2), the mailbox status channel (ADR-0046)
and the circuit-breaker / auto-disable logic (ADR-0026).
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as _dt
import uuid
from dataclasses import dataclass
from typing import Literal

import structlog
from cryptography.exceptions import InvalidTag

# NOTE: worker imports from ``backend.app.*`` (repositories + crm_push/oauth
# services) — this coupling is intentional and accepted by reviewers per the
# rework round 2 decision: keep `repositories/` in `backend/`. Both containers
# ship `backend/` + `worker/` + `shared/` per ``deploy/Dockerfile``.
from backend.app.crm_push.service import enqueue_crm_status_best_effort
from backend.app.exceptions import InvalidHostError
from backend.app.oauth.service import OAuthError, OAuthRefreshInvalidError, OutlookTokenService
from backend.app.repositories.mail_accounts import MailAccountsRepo
from backend.app.repositories.messages import MessagesRepo
from backend.app.security import assert_public_host_async
from shared.config import get_settings
from shared.crypto import decrypt_mail_password
from shared.db import make_session
from shared.logging import get_logger
from shared.models import MailAccount
from shared.redis_client import get_redis
from worker.app.error_classify import classify, error_prefix, is_explicit_permanent
from worker.app.imap_fetcher import FetchedBox, fetch_blocking

log = get_logger(__name__)

# ADR-0026: outcome of one account's sync, used by the two-phase
# ``_run_for_accounts`` to apply bump/disable AFTER the circuit-breaker
# decision (so a mass infra outage cannot disable everything at once).
AccountSyncOutcome = Literal["ok", "transient", "permanent"]


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

    Side-effect: updates the ``mail_accounts`` row + writes any new ``messages``
    (the CRM push-outbox, ADR-0043 §2). Attachments are not fetched (ADR-0043 §4).
    """
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
    #
    # TD-056: resolve OFF the event loop (``assert_public_host_async`` →
    # ``asyncio.to_thread``). The blocking ``socket.getaddrinfo`` used to run in
    # the worker's loop thread, so a hung resolver on ONE mailbox stalled the
    # ENTIRE sync cycle (all accounts), not just this one.
    try:
        await assert_public_host_async(account.imap_host, port=account.imap_port)
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

    # Save messages.
    new_count = 0
    conflict_count = 0
    # ADR-0043 §2: CRM push-outbox — EVERY newly-inserted message is pushed to
    # the CRM. Enqueued after COMMIT so the ``crm_push_dispatch`` job can load
    # the committed rows.
    crm_push_ids: list[int] = []
    async with make_session() as s, s.begin():
        repo = MessagesRepo(s)
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
            # ADR-0043 §2: every inserted message is an outbox item for the CRM.
            crm_push_ids.append(inserted_id)

    # Mark sync success.
    async with make_session() as s, s.begin():
        await MailAccountsRepo(s).mark_sync_success(
            account.id,
            last_synced_uidnext=box.uidnext,
            last_uidvalidity=box.uidvalidity,
        )

    # ADR-0043 §2: mirror the mailbox sync-status change (success:
    # ``last_synced_at=now()``, ``consecutive_failures=0``, ``last_sync_error=NULL``)
    # to the CRM. AFTER COMMIT — the dispatcher loads the live status snapshot
    # from the DB. No dedup: the ADR explicitly allows sending the status on
    # every cycle (idempotency "one alert per transition" lives in the CRM,
    # ``mail_accounts.down_alert_sent_at``). Gated + try/except inside.
    await enqueue_crm_status_best_effort(account.id)

    # ADR-0043 §2: enqueue every newly-inserted message onto ``crm_push_queue``
    # for delivery to the CRM. Gated by ``crm_push_enabled`` (URL + secret) so
    # a pre-cut-over deployment does not enqueue. Independent try/except — a
    # Redis outage must NEVER abort the sync cycle nor the other channels.
    if crm_push_ids and settings.crm_push_enabled:
        try:
            from backend.app.crm_push.service import enqueue_push_ids

            pushed = await enqueue_push_ids(crm_push_ids, source="sync")
            cycle_log.info(
                "crm_push_enqueued",
                count=pushed,
                mail_account_id=account.id,
            )
        except Exception as exc:
            cycle_log.warning(
                "crm_push_enqueue_failed",
                detail=str(exc)[:200],
                count=len(crm_push_ids),
            )

    cycle_log.info(
        "sync_account_finish",
        new_messages=new_count,
        conflicts=conflict_count,
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
            # ADR-0046 §3 (H7b): this branch short-circuits BEFORE the refresh,
            # so a mailbox that ALREADY carries ``oauth_needs_consent`` never
            # reaches the transition point (H7a) again. Without the marker it
            # would mirror to the CRM as green forever while not syncing at all.
            # Guarded + idempotent: writes (and pushes) at most once.
            await _record_needs_consent_marker(account.id)
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
    """Write ``last_sync_error`` without bumping the counter (ADR-0026 §2).

    ADR-0043 §2: this is one of the three sync-status update points, so after
    the COMMIT we mirror the new status to the CRM (best-effort, gated).
    """
    async with make_session() as s, s.begin():
        await MailAccountsRepo(s).mark_transient_error(account_id, error=error)

    # After COMMIT — the dispatcher loads the live status snapshot from the DB.
    await enqueue_crm_status_best_effort(account_id)


async def _record_failure(account_id: int, *, error: str, disable: bool) -> int:
    """Bump ``consecutive_failures`` (+ optional disable). Returns new count.

    Used in phase 2 of :func:`_run_for_accounts` for PERMANENT accounts when the
    circuit-breaker did NOT trip.

    ADR-0046 §3 (H3): ``mark_sync_failure`` writes two mirrored columns
    (``consecutive_failures``, ``last_sync_error``), so the new status is
    mirrored to the CRM AFTER the COMMIT — never inside the transaction (the
    dispatcher reads the live DB snapshot; enqueuing early would race and ship
    the pre-commit state).
    """
    async with make_session() as s, s.begin():
        failures = await MailAccountsRepo(s).mark_sync_failure(
            account_id, error=error, disable=disable
        )

    # After COMMIT — best-effort, gated (ADR-0046 §2).
    await enqueue_crm_status_best_effort(account_id)
    return failures


async def _record_needs_consent_marker(account_id: int) -> None:
    """Stamp the needs-consent marker on a clean-skipped mailbox (ADR-0046 §3 H7b).

    Guarded, idempotent single ``UPDATE`` (``last_sync_error IS DISTINCT FROM``
    the marker). The CRM hook fires ONLY when a row was really updated — a
    mailbox that already carries the marker produces neither a write nor a push,
    so a dead mailbox does not emit a status event every ``SYNC_INTERVAL``.
    Enqueue happens AFTER the COMMIT (ADR-0046 §2).
    """
    async with make_session() as s, s.begin():
        written = await MailAccountsRepo(s).mark_oauth_needs_consent_error(account_id)

    if written:
        await enqueue_crm_status_best_effort(account_id)


async def _disable_after_failures(account_id: int, *, user_id: int, reason: str) -> None:
    """Disable the mailbox after repeated failures (ADR-0026 §3).

    ``reason`` is a stable string: ``"N_consecutive_failures"`` (threshold) or
    ``"auth_failed"`` / ``"decrypt_fail"`` (explicit permanent, instant disable).

    ADR-0044 §4 (phase A3): the ``account_auto_disabled`` row in ``admin_audit``
    and the Telegram alert enqueue (``mailbox_alert_queue``, ADR-0033) are
    removed BEFORE the table/queue drops (§3 lock-step). ``disable_and_stamp_alert``
    is KEPT: it writes ``is_active=false`` plus the idempotency stamp; the alert
    itself is now sent by the CRM (ADR-0043 §2, ``down_alert_sent_at`` on its
    side).

    ADR-0046 §3 (H4): the mailbox status (``is_active`` true→false) is mirrored
    to the CRM AFTER the COMMIT — the dispatcher reads the live DB snapshot.
    """
    async with make_session() as s, s.begin():
        await MailAccountsRepo(s).disable_and_stamp_alert(account_id)

    log.info("mailbox_auto_disabled", mail_account_id=account_id, user_id=user_id, reason=reason)

    # After COMMIT — best-effort, gated (ADR-0046 §2).
    await enqueue_crm_status_best_effort(account_id)


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
        # ADR-0044 §4 (phase A3, MAJOR-4/MINOR-5): the audit row
        # ``sync_mass_failure_suppressed`` went away with ``admin_audit`` — the
        # breaker trip is still visible in the structured ``sync_breaker_tripped``
        # log line above (that write was the function's only effect).
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
