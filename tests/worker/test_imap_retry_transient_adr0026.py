"""ADR-0026 update §4 — sporadic transient IMAP error retry (imap_fetcher).

Scope A + B of the QA task for the ADR-0026 *update* (sporadic Microsoft
Outlook IMAP "User is authenticated but not connected" flake):

* Scope A — :func:`_is_retryable_imap_error` predicate: the transient
  ``imaplib.IMAP4.error`` / ``IMAP4.abort`` family ("authenticated but not
  connected" / "not connected" / "try again" / "temporarily" / "too many")
  returns ``True``, while PERMANENT auth markers (AUTHENTICATIONFAILED / login
  failed / invalid credentials / disabled / blocked) and non-IMAP exceptions
  return ``False``. The permanent check runs FIRST so a wrong-password reply that
  also happens to contain "not connected" is never retried.
* Scope B — :func:`_connect_and_login` retries the sporadic IMAP flake up to
  ``SYNC_CONNECT_RETRIES`` with backoff (mocked ``time.sleep``), returns the
  mailbox when a later attempt succeeds, re-raises a persistent flake after
  ``retries + 1`` attempts, and does NOT retry AUTHENTICATIONFAILED or a timeout.

PURE unit scope: ``imap_tools`` (the external boundary) is faked; no network,
no DB. ``time.sleep`` is patched so backoff never actually blocks.
"""

from __future__ import annotations

import imaplib
import socket
from typing import Any

import pytest

import worker.app.imap_fetcher as fetcher
from worker.app.imap_fetcher import _is_retryable_imap_error


class _FakeMailbox:
    """Minimal imap_tools.BaseMailBox stand-in for the XOAUTH2 / LOGIN path.

    ``xoauth2`` / ``login`` pop from a queue of outcomes: a ``None`` entry means
    "authenticate successfully", a ``BaseException`` entry is raised. ``logout``
    just counts (the retry loop best-effort closes the half-open socket).
    """

    def __init__(self, outcomes: list[BaseException | None]) -> None:
        self._outcomes = outcomes
        self.login_calls = 0
        self.logout_calls = 0

    def xoauth2(self, username: str, token: str, *, initial_folder: str = "INBOX") -> None:
        self._next()

    def login(self, username: str, password: str, *, initial_folder: str = "INBOX") -> None:
        self._next()

    def _next(self) -> None:
        self.login_calls += 1
        outcome = self._outcomes.pop(0) if self._outcomes else None
        if outcome is not None:
            raise outcome

    def logout(self) -> None:
        self.logout_calls += 1


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Backoff never actually blocks the test (deterministic, fast)."""
    monkeypatch.setattr(fetcher.time, "sleep", lambda _s: None)


def _install_mailbox(
    monkeypatch: pytest.MonkeyPatch, outcomes: list[BaseException | None]
) -> _FakeMailbox:
    box = _FakeMailbox(outcomes)
    monkeypatch.setattr(fetcher, "_open_mailbox", lambda **_kw: box)
    return box


def _set_retries(monkeypatch: pytest.MonkeyPatch, n: int) -> None:
    """Force SYNC_CONNECT_RETRIES regardless of env/.env.

    ADR-0028: ``_connect_and_login`` now also reads
    ``SYNC_OAUTH_LOGIN_FAILED_TRANSIENT`` to gate the OAuth login-failed retry.
    These ADR-0026 cases exercise the password / connect-flake path (where the
    flag is irrelevant), so we pin it to the production default ``True`` to keep
    the stub faithful and avoid an ``AttributeError`` on the new attribute.
    """

    class _S:
        SYNC_CONNECT_RETRIES = n
        SYNC_OAUTH_LOGIN_FAILED_TRANSIENT = True

    monkeypatch.setattr(fetcher, "get_settings", lambda: _S())


def _connect_oauth() -> Any:
    """XOAUTH2 path (the canonical Outlook flake case)."""
    return fetcher._connect_and_login(
        host="outlook.office365.com",
        port=993,
        ssl_on=True,
        username="u@example.com",
        password=None,
        access_token="tok",
        timeout=60,
    )


# ---------------------------------------------------------------------------
# Scope A — _is_retryable_imap_error predicate
# ---------------------------------------------------------------------------


class TestIsRetryableImapError:
    @pytest.mark.parametrize(
        "text",
        [
            "User is authenticated but not connected",
            "AUTHENTICATED BUT NOT CONNECTED",  # case-insensitive
            "server says: not connected, retry",
            "NO try again later",
            "[ALERT] service temporarily unavailable",
            "Too many simultaneous connections",
        ],
    )
    def test_transient_imap_error_is_retryable(self, text: str) -> None:
        assert _is_retryable_imap_error(imaplib.IMAP4.error(text)) is True

    def test_imap_abort_flake_is_retryable(self) -> None:
        # IMAP4.abort (dropped server connection) is also covered.
        assert _is_retryable_imap_error(imaplib.IMAP4.abort("not connected")) is True

    @pytest.mark.parametrize(
        "text",
        [
            "b'[AUTHENTICATIONFAILED] Invalid credentials (Failure)'",
            "LOGIN failed",
            "NO invalid credentials",
            "account is disabled",
            "account has been blocked",
        ],
    )
    def test_permanent_auth_marker_is_not_retryable(self, text: str) -> None:
        """CRITICAL: a permanent auth/account-state failure must NOT be retried
        even when it arrives as an IMAP4.error — a retry only wastes the cycle
        budget and the classifier must disable the account."""
        assert _is_retryable_imap_error(imaplib.IMAP4.error(text)) is False

    def test_permanent_marker_wins_over_transient_substring(self) -> None:
        """The permanent check runs FIRST: a reply that contains BOTH an auth
        marker and a transient substring ("not connected") is NOT retryable."""
        exc = imaplib.IMAP4.error("[AUTHENTICATIONFAILED] socket not connected")
        assert _is_retryable_imap_error(exc) is False

    @pytest.mark.parametrize(
        "exc",
        [
            ValueError("authenticated but not connected"),  # right text, wrong type
            TimeoutError("timed out"),
            RuntimeError("not connected"),
            OSError("not connected"),
        ],
    )
    def test_non_imap_exception_is_not_retryable(self, exc: BaseException) -> None:
        """Only imaplib.IMAP4.error / IMAP4.abort qualify — a non-IMAP exception
        with matching text must NOT be retried by this predicate (connect-level
        retries are handled by _is_retryable_connect_error)."""
        assert _is_retryable_imap_error(exc) is False

    def test_unknown_imap_error_text_is_not_retryable(self) -> None:
        # An IMAP4.error whose text matches no transient substring is not retried.
        assert _is_retryable_imap_error(imaplib.IMAP4.error("BAD command syntax")) is False


# ---------------------------------------------------------------------------
# Scope B — _connect_and_login retry behaviour for the IMAP flake
# ---------------------------------------------------------------------------


class TestConnectLoginImapFlakeRetry:
    def test_flake_then_success_returns_mailbox(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Sporadic flake on attempt 1, success on attempt 2 -> returns the
        authenticated mailbox; exactly 2 attempts."""
        _set_retries(monkeypatch, 3)
        flake = imaplib.IMAP4.error("User is authenticated but not connected")
        box = _install_mailbox(monkeypatch, [flake, None])
        result = _connect_oauth()
        assert result is box
        assert box.login_calls == 2

    def test_persistent_flake_raises_after_retries_plus_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A persistent flake exhausts SYNC_CONNECT_RETRIES (=3) and re-raises
        after retries + 1 = 4 attempts."""
        _set_retries(monkeypatch, 3)
        flake = imaplib.IMAP4.error("authenticated but not connected")
        box = _install_mailbox(monkeypatch, [flake, flake, flake, flake, flake])
        with pytest.raises(imaplib.IMAP4.error):
            _connect_oauth()
        assert box.login_calls == 4  # 1 initial + 3 retries

    def test_persistent_flake_default_retries_is_three(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Sanity-pin the documented default: SYNC_CONNECT_RETRIES defaults to 3
        so the loop covers the sporadic Outlook flake out of the box."""
        from shared.config import get_settings

        get_settings.cache_clear()
        assert get_settings().SYNC_CONNECT_RETRIES == 3
        # And the third backoff element exists for the 3rd retry (0.5/1.0/2.0).
        assert fetcher._RETRY_BACKOFFS == (0.5, 1.0, 2.0)

    def test_authenticationfailed_not_retried(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A real AUTHENTICATIONFAILED surfaced as an IMAP4.error must propagate
        on the FIRST attempt (no retry) so the classifier can disable."""
        _set_retries(monkeypatch, 3)
        auth = imaplib.IMAP4.error("[AUTHENTICATIONFAILED] Invalid credentials")
        box = _install_mailbox(monkeypatch, [auth, None])
        with pytest.raises(imaplib.IMAP4.error):
            _connect_oauth()
        assert box.login_calls == 1  # NO retry for permanent auth

    def test_timeout_not_retried(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A timeout during login is never retried (would multiply the wait)."""
        _set_retries(monkeypatch, 3)
        box = _install_mailbox(monkeypatch, [TimeoutError("timed out")])
        with pytest.raises((TimeoutError, socket.timeout)):
            _connect_oauth()
        assert box.login_calls == 1

    def test_flake_logs_out_half_open_socket_between_attempts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Each failed attempt best-effort closes its mailbox before retrying so
        we never leak a half-open socket (one logout per failed attempt)."""
        _set_retries(monkeypatch, 3)
        flake = imaplib.IMAP4.error("not connected")
        box = _install_mailbox(monkeypatch, [flake, None])
        _connect_oauth()
        # One failed attempt -> one logout (the successful attempt is not closed
        # by the retry loop; the caller owns logout of the returned mailbox).
        assert box.logout_calls == 1
