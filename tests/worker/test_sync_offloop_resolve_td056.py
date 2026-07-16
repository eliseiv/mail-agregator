"""TD-056 / ADR-0047 §4 — ``sync_one_account`` resolves OFF the worker's event loop.

Source of truth: ``docs/adr/ADR-0047-mailbox-test-hard-deadline.md`` §4
(``worker/app/sync_cycle.py:212``) and ``docs/05-modules.md`` §9.2 п.4:

    "in the worker a hung resolver stalled the WHOLE sync cycle (every mailbox),
     not just this one"

That is the regression this test pins: while ONE mailbox sits in a dead DNS
resolver, the worker's event loop must keep scheduling everything else — the
other mailboxes of the cycle included. A blocked loop cannot; an off-loop
(``asyncio.to_thread``) resolve can.
"""

from __future__ import annotations

from typing import Any

import pytest

from shared.crypto import encrypt_mail_password
from shared.models.mail_account import MailAccount
from tests.loop_probe import assert_loop_responsive_while, hung_getaddrinfo
from worker.app import sync_cycle

pytestmark = pytest.mark.worker

_ACCOUNT_ID = 777


@pytest.fixture
def prod_with_hung_dns(monkeypatch: pytest.MonkeyPatch) -> Any:
    """``APP_ENV=prod`` (the guard is a no-op elsewhere) + a resolver that hangs."""
    from backend.app import security as sec_mod
    from shared.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "APP_ENV", "prod")
    monkeypatch.setattr(sec_mod.socket, "getaddrinfo", hung_getaddrinfo())
    return settings


def _account() -> MailAccount:
    return MailAccount(
        id=_ACCOUNT_ID,
        user_id=1,
        email="hung@example.com",
        auth_type="password",
        encrypted_password=encrypt_mail_password("pw", _ACCOUNT_ID),
        imap_host="imap.example.com",
        imap_port=993,
        imap_ssl=True,
        smtp_host="smtp.example.com",
        smtp_port=587,
        smtp_ssl=False,
        smtp_starttls=True,
        is_active=True,
        oauth_needs_consent=False,
        consecutive_failures=0,
    )


async def test_hung_dns_on_one_mailbox_does_not_stall_the_cycle(
    prod_with_hung_dns: Any,
) -> None:
    """One mailbox stuck in ``getaddrinfo`` → the loop still schedules everyone else."""
    account = _account()

    async def _call() -> Any:
        return await sync_cycle.sync_one_account(
            account,
            timeout_seconds=30,
            initial_sync_days=7,
            max_body_bytes=1_000_000,
        )

    await assert_loop_responsive_while(_call)
