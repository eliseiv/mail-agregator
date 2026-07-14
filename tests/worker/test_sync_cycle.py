"""Worker tests for sync_cycle (post-decommission, ADR-0044 §4 phase A3).

Covered:

- ``_drain_forced_account_ids`` — force-sync markers are drained + deleted.
- Per-account timeout → TRANSIENT (ADR-0026): ``last_sync_error`` written, the
  counter NOT bumped, the mailbox NOT disabled — no matter how often it repeats.
- Decrypt failure → explicit PERMANENT (rule 9): instant disable.
- Successful sync → idempotent insert (``ON CONFLICT DO NOTHING``): a re-run of
  the same UID yields 0 new / 1 conflict.

ADR-0044 §4 (phase A3) removed every hook of the dismantled subsystems from the
cycle — tag-apply, the ``tg_notify`` / ``webhook`` / ``push_notify`` / ``forward``
enqueues, the MinIO attachment download and the ``admin_audit`` writers. The
assertions that rode on those (the ``TG_NOTIFY_ALL_MESSAGES`` enqueue gate, the
``account_auto_disabled`` audit rows) went with them: the OBSERVABLE outcome of
an auto-disable is now the mailbox state itself (``is_active`` /
``consecutive_failures`` / ``last_sync_error``), which is exactly what the CRM
reads over the status channel (ADR-0046 §3 H4). The surviving CRM push enqueue is
covered by ``test_push_enqueue_sync_cycle.py``.

Source of truth: ``worker/app/sync_cycle.py`` + ADR-0026 + ADR-0043 §2 + ADR-0044 §4.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from shared.crypto import encrypt_mail_password
from shared.models import MailAccount, User
from worker.app import sync_cycle as sc

pytestmark = pytest.mark.integration  # needs the DB + Redis to be live


@pytest.fixture
async def admin_user_with_account(
    db_engine: AsyncEngine,
) -> dict[str, Any]:
    """Pre-seed a user + a mail_account row."""
    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with factory() as ses, ses.begin():
        admin = User(
            username="sync_admin",
            role="super_admin",
            password_hash="$argon2id$v=19$m=65536,t=3,p=4$abc$def",
            password_reset_required=False,
        )
        ses.add(admin)
        await ses.flush()
        from backend.app.repositories.mail_accounts import MailAccountsRepo

        repo = MailAccountsRepo(ses)
        new_id = await repo.next_account_id()
        blob = encrypt_mail_password("p", new_id)
        acc = MailAccount(
            id=new_id,
            user_id=admin.id,
            email="sync@example.com",
            encrypted_password=blob,
            imap_host="imap.example.com",
            imap_port=993,
            imap_ssl=True,
            smtp_host="smtp.example.com",
            smtp_port=465,
            smtp_ssl=True,
            smtp_starttls=False,
        )
        ses.add(acc)
        await ses.flush()
        return {"user_id": admin.id, "account_id": acc.id}


class TestForceSyncDrain:
    async def test_drain_returns_account_ids_and_deletes_keys(self, redis_client: Any) -> None:
        await redis_client.set("force_sync:42", "1")
        await redis_client.set("force_sync:99", "1")
        await redis_client.set("unrelated:7", "x")
        ids = await sc._drain_forced_account_ids()
        assert ids == {42, 99}
        # Markers removed.
        assert await redis_client.get("force_sync:42") is None
        assert await redis_client.get("force_sync:99") is None
        # Unrelated key untouched.
        assert await redis_client.get("unrelated:7") == "x"


class TestSyncOneAccount:
    async def test_timeout_records_failure_and_does_not_raise(
        self,
        admin_user_with_account: dict[str, Any],
        db_engine: AsyncEngine,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Make the IMAP leg outlive the per-account timeout.
        async def _fake_to_thread(*_a: Any, **_k: Any) -> None:
            await asyncio.sleep(10)

        monkeypatch.setattr(asyncio, "to_thread", _fake_to_thread)

        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as ses:
            acc = await ses.get(MailAccount, admin_user_with_account["account_id"])
        assert acc is not None
        # ADR-0026: sync_one_account returns an _AccountResult and no longer bumps
        # the counter in the moment of error — a timeout is TRANSIENT (phase 0
        # writes last_sync_error only; bump/disable deferred to phase 2).
        result = await sc.sync_one_account(
            acc,
            timeout_seconds=1,
            initial_sync_days=30,
            max_body_bytes=1024,
            max_att_bytes=1024,
        )
        assert result.new_count == 0 and result.conflict_count == 0
        assert result.outcome == "transient"

        async with factory() as ses:
            acc2 = await ses.get(MailAccount, admin_user_with_account["account_id"])
        assert acc2 is not None
        # Transient: counter UNCHANGED, account NOT disabled, error recorded.
        assert acc2.consecutive_failures == 0
        assert acc2.is_active is True
        assert acc2.last_sync_error is not None
        assert "timeout" in acc2.last_sync_error.lower()

    async def test_repeated_timeouts_never_disable_transient(
        self,
        admin_user_with_account: dict[str, Any],
        db_engine: AsyncEngine,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """ADR-0026 root-cause-A fix: timeouts are TRANSIENT and must NEVER disable.

        Pre-ADR-0026 three consecutive timeouts auto-disabled the mailbox — exactly
        the over-eager disable the ADR removed. Run the FULL two-phase
        ``_run_for_accounts`` three times; the account must stay active and the
        counter must stay 0. (Permanent-only threshold disable is covered in
        ``test_breaker_repo_adr0026.py``.)
        """

        # Deterministically surface a timeout the way a real IMAP stall does:
        # ``asyncio.wait_for`` raises ``TimeoutError`` when the to_thread call
        # exceeds the cycle's ``IMAP_TIMEOUT_SECONDS``. We raise it directly so
        # the test exercises the timeout->transient path regardless of the
        # configured timeout (sleeping less than IMAP_TIMEOUT_SECONDS would
        # instead let ``fetch_blocking`` "return" None and raise an unrelated
        # AttributeError, which masks what this test is asserting).
        async def _fake_to_thread(*_a: Any, **_k: Any) -> None:
            raise TimeoutError("timed out")

        monkeypatch.setattr(asyncio, "to_thread", _fake_to_thread)
        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)

        for _ in range(3):
            async with factory() as ses:
                acc = await ses.get(MailAccount, admin_user_with_account["account_id"])
            assert acc is not None
            await sc._run_for_accounts([acc])

        async with factory() as ses:
            acc2 = await ses.get(MailAccount, admin_user_with_account["account_id"])
        assert acc2 is not None
        assert acc2.is_active is True  # transient never disables
        assert acc2.consecutive_failures == 0  # transient never bumps
        assert acc2.last_sync_error is not None
        assert "timeout" in acc2.last_sync_error.lower()

    async def test_decrypt_failure_disables_account(
        self,
        admin_user_with_account: dict[str, Any],
        db_engine: AsyncEngine,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from cryptography.exceptions import InvalidTag

        def _bad_decrypt(*_a: Any, **_k: Any) -> str:
            raise InvalidTag("corrupt blob")

        monkeypatch.setattr(sc, "decrypt_mail_password", _bad_decrypt)

        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as ses:
            acc = await ses.get(MailAccount, admin_user_with_account["account_id"])
        assert acc is not None
        # ADR-0026: decrypt failure is an EXPLICIT PERMANENT (rule 9) — instant
        # disable, but the disable happens in phase 2. Run the full cycle (single
        # account, breaker not engaged) so phase 0 classifies + phase 2 disables.
        await sc._run_for_accounts([acc])

        async with factory() as ses:
            acc2 = await ses.get(MailAccount, admin_user_with_account["account_id"])
        assert acc2 is not None
        # ADR-0044 §4: the ``account_auto_disabled`` audit row went away with
        # ``admin_audit`` — the disable is now observable in the mailbox state
        # alone, which is what the CRM mirrors (ADR-0046 §3 H4).
        assert acc2.is_active is False  # explicit-permanent => instant disable
        assert acc2.consecutive_failures == 1  # bumped once before disable
        assert acc2.last_sync_error is not None
        assert "decrypt" in acc2.last_sync_error.lower()
        # ``disable_and_stamp_alert`` is KEPT (ADR-0044 §4): the idempotency stamp
        # still lands, only the Telegram alert enqueue is gone (the CRM alerts now).
        assert acc2.disabled_alert_sent_at is not None

    async def test_successful_sync_persists_messages_via_idempotent_insert(
        self,
        admin_user_with_account: dict[str, Any],
        db_engine: AsyncEngine,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Build a fake FetchedBox with one message. ADR-0044 §4: attachments are
        # neither fetched nor stored any more — the box carries none.
        from datetime import UTC
        from datetime import datetime as _dt

        from worker.app.imap_fetcher import FetchedBox, FetchedMessage

        fetched_box = FetchedBox(
            uidvalidity=42,
            uidnext=101,
            new_messages=[
                FetchedMessage(
                    uid=100,
                    message_id_header="<mid@x>",
                    from_addr="x@y.com",
                    from_name="X",
                    to_addrs="sync@example.com",
                    cc_addrs=None,
                    subject="hello",
                    internal_date=_dt.now(UTC),
                    body_text="hi",
                    body_html=None,
                    body_truncated=False,
                    body_present=True,
                    in_reply_to=None,
                    refs_header=None,
                    x_forwarded_by=None,
                    attachments=[],
                )
            ],
        )

        async def _fake_to_thread(_func: Any, *_a: Any, **_k: Any) -> Any:
            return fetched_box

        monkeypatch.setattr(asyncio, "to_thread", _fake_to_thread)

        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as ses:
            acc = await ses.get(MailAccount, admin_user_with_account["account_id"])
        assert acc is not None

        # First run: 1 new message inserted.
        res1 = await sc.sync_one_account(
            acc,
            timeout_seconds=10,
            initial_sync_days=30,
            max_body_bytes=1024,
            max_att_bytes=1024 * 1024,
        )
        assert res1.new_count == 1
        assert res1.conflict_count == 0
        assert res1.outcome == "ok"

        # Second run: same UID -> ON CONFLICT DO NOTHING => 0 new, 1 conflict.
        async with factory() as ses:
            acc2 = await ses.get(MailAccount, admin_user_with_account["account_id"])
        assert acc2 is not None
        res2 = await sc.sync_one_account(
            acc2,
            timeout_seconds=10,
            initial_sync_days=30,
            max_body_bytes=1024,
            max_att_bytes=1024 * 1024,
        )
        assert res2.new_count == 0
        assert res2.conflict_count == 1
