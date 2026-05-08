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
import uuid
from dataclasses import dataclass

import structlog
from cryptography.exceptions import InvalidTag

# NOTE: worker imports from ``backend.app.*`` (repositories + audit) — this
# coupling is intentional and accepted by reviewers per the rework round 2
# decision: keep `repositories/` in `backend/` to avoid moving 6 files
# without architect sign-off. Both containers ship `backend/` + `worker/` +
# `shared/` per ``deploy/Dockerfile``.
from backend.app.audit import AuditWriter
from backend.app.exceptions import InvalidHostError
from backend.app.repositories.mail_accounts import MailAccountsRepo
from backend.app.repositories.messages import MessagesRepo
from backend.app.repositories.users import UsersRepo
from backend.app.security import assert_public_host
from backend.app.tags.service import TagsService
from shared.config import get_settings
from shared.crypto import decrypt_mail_password
from shared.db import make_session
from shared.logging import get_logger
from shared.models import MailAccount
from shared.redis_client import get_redis
from shared.storage import get_storage
from worker.app.imap_fetcher import FetchedBox, fetch_blocking

log = get_logger(__name__)

# Tag for invalid auth — gets ``is_active=false`` immediately (per ADR-0008).
_AUTH_FAIL_PREFIX = "auth_failed"
_DISABLE_AFTER_FAILS = 3


@dataclass(slots=True)
class _TagInputMessage:
    """Minimal message-shaped tuple passed to ``TagsService.apply_tags_to_message``.

    The service expects a ``Message``-shaped object with ``id``, ``subject``,
    ``body_text`` and ``from_addr``. Constructing a real ORM ``Message`` here
    would require extra round-trips (we already have the values from
    ``FetchedMessage`` + ``inserted_id``); a tiny dataclass keeps the call
    clean and avoids a SELECT round-trip.
    """

    id: int
    subject: str | None
    body_text: str
    from_addr: str


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
) -> tuple[int, int]:
    """Sync one account. Returns ``(new_messages_count, conflict_count)``.

    Side-effect: updates ``mail_accounts`` row + writes any new ``messages``
    + ``attachments`` + uploads attachment blobs to MinIO.
    """
    storage = get_storage()
    cycle_log = log.bind(mail_account_id=account.id, user_id=account.user_id)
    cycle_log.info("sync_account_start")

    try:
        password = decrypt_mail_password(account.encrypted_password, account.id)
    except InvalidTag as exc:
        cycle_log.error("sync_account_decrypt_fail", detail=str(exc)[:200])
        await _record_failure(
            account.id,
            error="decrypt_fail",
            disable=True,
        )
        return 0, 0

    # SSRF guard per ``docs/06-security.md`` sec. 4: backend (test) AND worker
    # (sync) must verify the host doesn't resolve to a private network. Guards
    # against DNS-rebinding and tampered DB rows pointing at internal hosts.
    # No-op in dev (so localhost mock IMAP servers still work).
    try:
        assert_public_host(account.imap_host, port=account.imap_port)
    except InvalidHostError as exc:
        detail = str(exc.message) if hasattr(exc, "message") else str(exc)
        cycle_log.warning("sync_account_invalid_host", detail=detail[:200])
        await _record_failure(
            account.id,
            error=f"invalid_host: {detail[:200]}",
            disable=True,
        )
        return 0, 0

    try:
        box: FetchedBox = await asyncio.wait_for(
            asyncio.to_thread(
                fetch_blocking,
                host=account.imap_host,
                port=account.imap_port,
                ssl_on=account.imap_ssl,
                username=account.email,
                password=password,
                last_synced_uidnext=account.last_synced_uidnext,
                last_uidvalidity=account.last_uidvalidity,
                initial_sync_days=initial_sync_days,
                max_body_bytes=max_body_bytes,
                max_att_bytes=max_att_bytes,
                timeout=timeout_seconds,
            ),
            timeout=timeout_seconds,
        )
    except TimeoutError:
        cycle_log.warning("sync_account_timeout")
        # ADR-0008 + ``docs/05-modules.md`` sec. 14: auto-disable after 3
        # consecutive failures applies to ANY repeat failure, including
        # timeouts (otherwise persistent network/firewall blocks would never
        # auto-disable).
        failed = await _record_failure(
            account.id, error=f"timeout_{timeout_seconds}s", disable=False
        )
        if failed >= _DISABLE_AFTER_FAILS:
            cycle_log.warning(
                "sync_account_auto_disabled",
                consecutive_failures=failed,
            )
            await _disable_after_failures(account.id, failed=failed, user_id=account.user_id)
        return 0, 0
    except Exception as exc:
        msg = type(exc).__name__
        text = str(exc).replace("\r", " ").replace("\n", " ")[:200]
        full = f"{msg}: {text}"
        # Distinguish auth failures (which should disable) from network blips.
        is_auth = "AUTHENTICATIONFAILED" in text.upper() or "LOGIN" in msg.upper()
        if is_auth or "MailboxLoginError" in msg:
            cycle_log.warning("sync_account_auth_fail", detail=text)
            await _record_failure(
                account.id,
                error=f"{_AUTH_FAIL_PREFIX}: {text}",
                disable=True,
            )
        else:
            cycle_log.warning("sync_account_error", detail=full)
            failed = await _record_failure(account.id, error=full, disable=False)
            if failed >= _DISABLE_AFTER_FAILS:
                cycle_log.warning(
                    "sync_account_auto_disabled",
                    consecutive_failures=failed,
                )
                # Re-mark as disabled and write audit.
                await _disable_after_failures(account.id, failed=failed, user_id=account.user_id)
        return 0, 0

    # Save messages + attachments.
    new_count = 0
    conflict_count = 0
    tags_applied_total = 0
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
                body_truncated=fmsg.body_truncated,
                body_present=fmsg.body_present,
                in_reply_to=fmsg.in_reply_to,
                refs_header=fmsg.refs_header,
            )
            if inserted_id is None:
                conflict_count += 1
                continue
            new_count += 1
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
                        from_addr=fmsg.from_addr,
                    ),
                    user_id=account.user_id,
                )
                tags_applied_total += applied
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

    cycle_log.info(
        "sync_account_finish",
        new_messages=new_count,
        conflicts=conflict_count,
        tags_applied=tags_applied_total,
    )
    return new_count, conflict_count


# ---------------------------------------------------------------------------
# Helpers used by sync_one_account
# ---------------------------------------------------------------------------


async def _record_failure(account_id: int, *, error: str, disable: bool) -> int:
    """Write the failure to ``mail_accounts``. Returns new ``consecutive_failures``."""
    async with make_session() as s, s.begin():
        return await MailAccountsRepo(s).mark_sync_failure(account_id, error=error, disable=disable)


async def _disable_after_failures(account_id: int, *, failed: int, user_id: int) -> None:
    """Disable the account and write an audit row."""
    async with make_session() as s, s.begin():
        await MailAccountsRepo(s).update_fields(account_id, is_active=False)
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
                "reason": f"{failed}_consecutive_failures",
            },
        )


# ---------------------------------------------------------------------------
# Cycle / dispatcher
# ---------------------------------------------------------------------------


async def _run_for_accounts(accounts: list[MailAccount]) -> tuple[int, int, int]:
    """Run :func:`sync_one_account` for each account under the IMAP semaphore.

    Returns ``(accounts_ok, accounts_failed, new_messages)``. Exceptions from
    individual accounts are captured (``return_exceptions=True``) so a single
    failure can never abort the others — guaranteed by ``docs/05-modules.md``
    sec. 14 ("Failure одного аккаунта не валит остальные").
    """
    if not accounts:
        return 0, 0, 0

    settings = get_settings()
    sem = asyncio.Semaphore(settings.MAX_CONCURRENT_IMAP)

    async def _bounded(acc: MailAccount) -> tuple[int, int]:
        async with sem:
            return await sync_one_account(
                acc,
                timeout_seconds=settings.IMAP_TIMEOUT_SECONDS,
                initial_sync_days=settings.INITIAL_SYNC_DAYS,
                max_body_bytes=settings.MAX_BODY_BYTES,
                max_att_bytes=settings.MAX_ATTACHMENT_BYTES,
            )

    results = await asyncio.gather(*[_bounded(a) for a in accounts], return_exceptions=True)

    ok = 0
    failed = 0
    new_msgs = 0
    for res in results:
        if isinstance(res, BaseException):
            failed += 1
            continue
        new_count, _conflicts = res
        ok += 1
        new_msgs += new_count
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
