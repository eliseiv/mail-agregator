"""Prod-bug regression (2026-07-15) — the LOGIN/SECRET the SMTP transport receives.

Source of truth: ``backend/app/send/service.py::smtp_send_message`` (password
branch: ``smtp_user = normalize_optional_login(account.smtp_username) or
account.email``; ``smtp_pwd`` = ``normalize_optional_secret`` of the decrypted
``smtp_encrypted_password``, falling back to the decrypted mandatory
``encrypted_password`` — the latter NEVER normalised) and
``backend/app/accounts/service.py`` (the ``_test_existing_account`` probe, which
MUST resolve the same login/secret as the send).

These are the DIRECT regressions of the incident. Rather than assert the
resolution indirectly, each test drives the real code path and records the exact
``username`` / ``password`` handed to the third-party boundary:

- for SEND — ``aiosmtplib.send`` (the only mock; the SSRF re-resolve is stubbed
  to a no-op so the test is offline and deterministic);
- for the PROBE — ``smtp_test_login`` (accounts.service's SMTP probe helper).

The prod row that broke everything was ``smtp_username == 'None'`` (literal text),
so the headline case asserts the wire username is the mailbox address, NOT ``None``.
"""

from __future__ import annotations

from email.message import EmailMessage
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.accounts import service as acct_svc
from backend.app.accounts.service import MailAccountService
from backend.app.deps import VisibilityScope
from backend.app.send import service as snd_svc
from shared.crypto import encrypt_mail_password
from shared.models import MailAccount

pytestmark = pytest.mark.unit

_ACCOUNT_ID = 4242
_EMAIL = "box@example.com"
_IMAP_PW = "imap-secret"


# ===========================================================================
# Fixtures — build an in-memory account + capture the SMTP transport call
# ===========================================================================


def _account(
    *,
    smtp_username: str | None,
    imap_password: str = _IMAP_PW,
    smtp_password: str | None = None,
) -> MailAccount:
    """A password account, never persisted (the send resolves in-memory).

    ``smtp_password`` (when given) is stored ENCRYPTED under ``account.id`` — the
    same AAD binding the real row uses, so the decrypt in ``smtp_send_message``
    round-trips it.
    """
    smtp_enc = (
        encrypt_mail_password(smtp_password, _ACCOUNT_ID) if smtp_password is not None else None
    )
    return MailAccount(
        id=_ACCOUNT_ID,
        user_id=1,
        email=_EMAIL,
        auth_type="password",
        encrypted_password=encrypt_mail_password(imap_password, _ACCOUNT_ID),
        smtp_encrypted_password=smtp_enc,
        smtp_username=smtp_username,
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


@pytest.fixture
def smtp_recorder(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Stub the SSRF re-resolve + ``aiosmtplib.send``; record username/password.

    The SSRF guard is stubbed so no name is resolved and no packet leaves; the
    transport is stubbed so no e-mail is sent. Everything BETWEEN — the
    credential resolution under test — runs for real.
    """
    import aiosmtplib

    rec: dict[str, Any] = {"calls": 0, "username": "SENTINEL", "password": "SENTINEL"}

    async def _no_ssrf(*_a: Any, **_k: Any) -> None:
        return None

    async def _fake_send(*_a: Any, **kwargs: Any) -> None:
        rec["calls"] += 1
        rec["username"] = kwargs.get("username")
        rec["password"] = kwargs.get("password")

    monkeypatch.setattr(snd_svc, "assert_public_host_async", _no_ssrf)
    monkeypatch.setattr(aiosmtplib, "send", _fake_send)
    return rec


async def _send(account: MailAccount) -> None:
    # Password branch never touches the session before the transport call.
    await snd_svc.smtp_send_message(
        account,
        EmailMessage(),
        ["dest@example.com"],
        session=cast("AsyncSession", None),
    )


# ===========================================================================
# 1. HEADLINE — smtp_username == 'None' (the literal prod value) → login = email
# ===========================================================================


class TestSendUsernameResolution:
    async def test_literal_none_username_logs_in_as_the_mailbox_address(
        self, smtp_recorder: dict[str, Any]
    ) -> None:
        """The 41-row prod bug: ``'None'`` must NOT reach the wire as the login."""
        await _send(_account(smtp_username="None"))
        assert smtp_recorder["calls"] == 1
        assert smtp_recorder["username"] == _EMAIL
        assert smtp_recorder["username"] != "None"

    @pytest.mark.parametrize("garbage", ["", "   ", "null", "NONE", "undefined", "NoNe"])
    async def test_every_absence_sentinel_falls_back_to_email(
        self, smtp_recorder: dict[str, Any], garbage: str
    ) -> None:
        await _send(_account(smtp_username=garbage))
        assert smtp_recorder["username"] == _EMAIL

    async def test_a_real_smtp_username_is_used_as_is(self, smtp_recorder: dict[str, Any]) -> None:
        """The 3 prod rows with a REAL distinct login must not regress."""
        await _send(_account(smtp_username="postmaster@example.com"))
        assert smtp_recorder["username"] == "postmaster@example.com"

    async def test_a_real_username_with_surrounding_spaces_is_trimmed(
        self, smtp_recorder: dict[str, Any]
    ) -> None:
        await _send(_account(smtp_username="  postmaster@example.com  "))
        assert smtp_recorder["username"] == "postmaster@example.com"


# ===========================================================================
# 2. SECRET resolution — sentinel → IMAP-password fallback; real secret verbatim
# ===========================================================================


class TestSendSecretResolution:
    async def test_smtp_password_none_falls_back_to_the_imap_password(
        self, smtp_recorder: dict[str, Any]
    ) -> None:
        # Stored smtp secret is the literal 'None' -> absence -> fall back to the
        # mandatory IMAP password (``encrypted_password``).
        await _send(_account(smtp_username=None, smtp_password="None"))
        assert smtp_recorder["password"] == _IMAP_PW

    async def test_blank_smtp_password_falls_back_to_the_imap_password(
        self, smtp_recorder: dict[str, Any]
    ) -> None:
        await _send(_account(smtp_username=None, smtp_password="   "))
        assert smtp_recorder["password"] == _IMAP_PW

    async def test_no_stored_smtp_password_uses_the_imap_password(
        self, smtp_recorder: dict[str, Any]
    ) -> None:
        await _send(_account(smtp_username=None, smtp_password=None))
        assert smtp_recorder["password"] == _IMAP_PW

    async def test_a_real_smtp_password_with_edge_spaces_is_sent_verbatim(
        self, smtp_recorder: dict[str, Any]
    ) -> None:
        """Secrets are opaque: surrounding spaces are SIGNIFICANT, never trimmed."""
        await _send(_account(smtp_username=None, smtp_password="  p@ss word  "))
        assert smtp_recorder["password"] == "  p@ss word  "

    async def test_a_plain_real_smtp_password_is_used(self, smtp_recorder: dict[str, Any]) -> None:
        await _send(_account(smtp_username=None, smtp_password="dedicated-smtp-pw"))
        assert smtp_recorder["password"] == "dedicated-smtp-pw"


# ===========================================================================
# 3. The MANDATORY IMAP password is NOT normalised (case 5)
# ===========================================================================


class TestMandatoryImapPasswordNotNormalised:
    async def test_imap_password_literal_none_is_sent_verbatim_not_masked(
        self, smtp_recorder: dict[str, Any]
    ) -> None:
        """A genuinely broken credential (IMAP pw == 'None') must NOT be hidden.

        The optional SMTP fields are normalised (absence → fallback); the
        mandatory ``encrypted_password`` is NOT — so a real ``'None'`` there stays
        ``'None'`` and the send fails loudly (535) instead of silently doing
        something else. Masking it would hide a real credential error.
        """
        # No SMTP username/password -> both legs fall back to the IMAP password,
        # which is itself the literal 'None'.
        await _send(_account(smtp_username=None, imap_password="None", smtp_password=None))
        assert smtp_recorder["username"] == _EMAIL  # username still falls back
        assert smtp_recorder["password"] == "None"  # secret NOT normalised


# ===========================================================================
# 4. PROBE == SEND — "Проверить соединение" resolves IDENTICALLY (case 6)
# ===========================================================================


class _FakeRepo:
    """Minimal stand-in for ``MailAccountsRepo`` used by ``_test_existing_account``."""

    def __init__(self, account: MailAccount) -> None:
        self._account = account

    async def get_for_user_ids(
        self, _user_ids: list[int] | None, account_id: int
    ) -> MailAccount | None:
        return self._account if account_id == self._account.id else None


@pytest.fixture
def probe_recorder(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Stub the two probe helpers in accounts.service; record the SMTP creds.

    ``imap_test_login`` / ``smtp_test_login`` are imported by name INTO
    accounts.service, so the patch targets that namespace.
    """
    rec: dict[str, Any] = {"username": "SENTINEL", "password": "SENTINEL"}

    async def _fake_imap(**_k: Any) -> None:
        return None

    async def _fake_smtp(**kwargs: Any) -> None:
        rec["username"] = kwargs.get("username")
        rec["password"] = kwargs.get("password")

    monkeypatch.setattr(acct_svc, "imap_test_login", _fake_imap)
    monkeypatch.setattr(acct_svc, "smtp_test_login", _fake_smtp)
    return rec


async def _probe(account: MailAccount) -> None:
    """Drive ``MailAccountService._test_existing_account`` without a DB session.

    ``__new__`` bypasses ``__init__`` (which registers a session guard needing a
    real session); the password-probe path touches only ``_repo`` + the patched
    probe helpers, never ``self._db``.
    """
    svc = MailAccountService.__new__(MailAccountService)
    svc._db = cast("AsyncSession", None)
    svc._repo = cast(Any, _FakeRepo(account))
    scope = VisibilityScope(user_id=1, role="super_admin", group_id=None, group_ids=frozenset())
    await svc._test_existing_account(scope, account.id)


class TestProbeMatchesSend:
    @pytest.mark.parametrize(
        ("smtp_username", "smtp_password", "imap_password"),
        [
            ("None", "None", _IMAP_PW),  # the exact prod shape
            (None, None, _IMAP_PW),  # no optional creds at all
            ("real@login", "dedicated-pw", _IMAP_PW),  # both real, distinct
            ("", "   ", _IMAP_PW),  # blank sentinels
            (None, "  spaced-pw  ", _IMAP_PW),  # secret with significant spaces
        ],
    )
    async def test_probe_resolves_the_same_login_and_secret_as_send(
        self,
        smtp_recorder: dict[str, Any],
        probe_recorder: dict[str, Any],
        smtp_username: str | None,
        smtp_password: str | None,
        imap_password: str,
    ) -> None:
        """A green "Проверить соединение" must never diverge from a real send."""
        send_account = _account(
            smtp_username=smtp_username,
            smtp_password=smtp_password,
            imap_password=imap_password,
        )
        await _send(send_account)

        probe_account = _account(
            smtp_username=smtp_username,
            smtp_password=smtp_password,
            imap_password=imap_password,
        )
        await _probe(probe_account)

        assert probe_recorder["username"] == smtp_recorder["username"]
        assert probe_recorder["password"] == smtp_recorder["password"]

    async def test_probe_of_the_prod_row_logs_in_as_email_with_the_imap_pw(
        self, probe_recorder: dict[str, Any]
    ) -> None:
        """Pin the absolute values for the incident row (not just send==probe)."""
        await _probe(_account(smtp_username="None", smtp_password="None"))
        assert probe_recorder["username"] == _EMAIL
        assert probe_recorder["password"] == _IMAP_PW
