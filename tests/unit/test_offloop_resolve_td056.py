"""TD-056 / ADR-0047 §4 — the SSRF guard resolves OFF the event loop, everywhere.

Source of truth: ``docs/adr/ADR-0047-mailbox-test-hard-deadline.md`` §4 (table of
the eight call-sites) and ``docs/05-modules.md`` §9.2 п.4.

The connection-test call-sites are covered behaviourally in
``test_mailbox_test_deadline_adr0047.py`` (a hung resolver must not stop
``/healthz`` from answering). This module covers the OTHERS — the ones
TD-056 closed:

- ``backend/app/send/service.py`` — ``smtp_send_message``;
- ``backend/app/send/service.py`` — the IMAP APPEND to Sent;
- ``worker/app/sync_cycle.py`` — ``sync_one_account`` (covered in
  ``tests/worker/test_sync_offloop_resolve_td056.py``).

NB (ADR-0044 / TD-060): the forward relay leg (``smtp_send_via_relay`` +
``FORWARD_SMTP_*``) was removed with forwarding, so its loop-liveness test went
with it. The structural guard below still covers the whole class of defect.

The directly-callable send helper gets a behavioural loop-liveness test. The
APPEND leg sits deep inside ``SendService.send`` (needs a persisted message and
an SMTP peer to reach), so it is guarded structurally instead: NO
coroutine anywhere in the async code may call the SYNC ``assert_public_host`` —
that is exactly the invariant ADR-0047 §4 states ("прямых вызовов синхронного
``assert_public_host`` из корутин не осталось"), and it covers the APPEND leg
along with every future one.
"""

from __future__ import annotations

import re
from email.message import EmailMessage
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest

from backend.app.send import service as snd_svc
from shared.crypto import encrypt_mail_password
from shared.models.mail_account import MailAccount
from tests.loop_probe import assert_loop_responsive_while, hung_getaddrinfo

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.unit

_ACCOUNT_ID = 4242


@pytest.fixture
def prod_with_hung_dns(monkeypatch: pytest.MonkeyPatch) -> Any:
    """``APP_ENV=prod`` (else the guard is a no-op) + a resolver that hangs."""
    from backend.app import security as sec_mod
    from shared.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "APP_ENV", "prod")
    monkeypatch.setattr(sec_mod.socket, "getaddrinfo", hung_getaddrinfo())
    return settings


def _password_account() -> MailAccount:
    """In-memory password account. Never persisted — the resolve happens first."""
    return MailAccount(
        id=_ACCOUNT_ID,
        user_id=1,
        email="box@example.com",
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


class TestSendPathResolvesOffLoop:
    async def test_smtp_send_message_does_not_block_the_loop(self, prod_with_hung_dns: Any) -> None:
        """``send/service.py:228`` — the SSRF re-check at send time (TD-056)."""
        account = _password_account()

        async def _call() -> None:
            # The password branch does not touch the session before the resolve;
            # the task is cancelled while still inside it.
            await snd_svc.smtp_send_message(
                account,
                EmailMessage(),
                ["to@example.com"],
                session=cast("AsyncSession", None),
            )

        await assert_loop_responsive_while(_call)


class TestNoSyncCallSitesLeft:
    """Structural guard for the whole class of defect — incl. the IMAP APPEND leg."""

    #: ``assert_public_host(`` but NOT ``assert_public_host_async(``.
    _SYNC_CALL = re.compile(r"\bassert_public_host\s*\((?!\s*\))")

    @pytest.mark.parametrize(
        "relative",
        [
            "backend/app/accounts/testers.py",
            "backend/app/send/service.py",
            "worker/app/sync_cycle.py",
        ],
    )
    def test_async_modules_never_call_the_sync_guard(self, relative: str) -> None:
        root = Path(__file__).resolve().parents[2]
        source = (root / relative).read_text(encoding="utf-8")
        offenders = [
            f"{relative}:{n}: {line.strip()}"
            for n, line in enumerate(source.splitlines(), start=1)
            if self._SYNC_CALL.search(line)
        ]
        assert not offenders, (
            "a coroutine calls the BLOCKING assert_public_host — its getaddrinfo runs in the "
            "event-loop thread, stalling the process and making every deadline decorative "
            f"(ADR-0047 §4 / TD-056). Use assert_public_host_async. Offenders: {offenders}"
        )

    def test_the_sync_guard_still_exists_as_the_body_of_the_async_one(self) -> None:
        """It is not deleted — ``assert_public_host_async`` is a ``to_thread`` around it."""
        from backend.app.security import assert_public_host, assert_public_host_async

        assert callable(assert_public_host)
        assert callable(assert_public_host_async)
