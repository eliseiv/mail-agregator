"""ADR-0028 §3 — OAuth-gated IMAP "login failed" retry (imap_fetcher).

PURE unit scope (no network, no DB; ``imap_tools`` faked, ``time.sleep`` patched):

* :func:`_is_retryable_imap_error` predicate, both substring sets:
  - ``oauth=True``: "login failed" / "authenticationfailed" become RETRYABLE
    (Microsoft server flake on a refresh-verified token), alongside the existing
    transient family ("authenticated but not connected" / "try again" / ...).
  - ``oauth=False`` (password path — UNCHANGED): the SAME "login failed" /
    "authenticationfailed" stay PERMANENT and are NOT retried (a wrong password
    must propagate so the classifier disables). The disabled/blocked/invalid-
    credentials markers stay permanent on BOTH paths.
* :func:`_connect_and_login` end-to-end retry behaviour + the
  ``SYNC_OAUTH_LOGIN_FAILED_TRANSIENT`` kill-switch: with the flag ON the
  XOAUTH2 path retries an IMAP "login failed"; with the flag OFF the oauth
  gate is disabled and the same flake propagates on the first attempt.

Mirrors ``tests/worker/test_imap_retry_transient_adr0026.py`` (the ADR-0026
sibling) so the two ADRs share the same fake-mailbox harness style.
"""

from __future__ import annotations

import imaplib
from typing import Any

import pytest

import worker.app.imap_fetcher as fetcher
from worker.app.imap_fetcher import _is_retryable_imap_error


class _FakeMailbox:
    """imap_tools.BaseMailBox stand-in: ``xoauth2``/``login`` pop an outcome
    queue (``None`` => authenticate OK, exception => raise); ``logout`` counts."""

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


def _set_settings(monkeypatch: pytest.MonkeyPatch, *, retries: int, kill_switch: bool) -> None:
    """Force SYNC_CONNECT_RETRIES + SYNC_OAUTH_LOGIN_FAILED_TRANSIENT inside the
    fetcher only (independent of env / .env)."""

    class _S:
        SYNC_CONNECT_RETRIES = retries
        SYNC_OAUTH_LOGIN_FAILED_TRANSIENT = kill_switch

    monkeypatch.setattr(fetcher, "get_settings", lambda: _S())


def _connect_oauth() -> Any:
    """XOAUTH2 path: access_token set => ``oauth`` gate engaged in the fetcher."""
    return fetcher._connect_and_login(
        host="outlook.office365.com",
        port=993,
        ssl_on=True,
        username="u@example.com",
        password=None,
        access_token="tok",
        timeout=60,
    )


def _connect_password() -> Any:
    """LOGIN path: password set, access_token None => ``oauth`` gate OFF."""
    return fetcher._connect_and_login(
        host="imap.example.com",
        port=993,
        ssl_on=True,
        username="u@example.com",
        password="secret",
        access_token=None,
        timeout=60,
    )


# ---------------------------------------------------------------------------
# Scope A — _is_retryable_imap_error(exc, oauth=...) predicate
# ---------------------------------------------------------------------------


class TestIsRetryableImapErrorOAuthGate:
    @pytest.mark.parametrize(
        "text",
        [
            "LOGIN failed.",
            "b'LOGIN failed.'",
            "[AUTHENTICATIONFAILED] LOGIN failed.",
            "AUTHENTICATIONFAILED",
            "no AuthenticationFailed here",  # case-insensitive substring
        ],
    )
    def test_oauth_login_failed_is_retryable(self, text: str) -> None:
        """oauth=True: the Microsoft auth flake is retried (ADR-0028 §3)."""
        assert _is_retryable_imap_error(imaplib.IMAP4.error(text), oauth=True) is True

    @pytest.mark.parametrize(
        "text",
        [
            "LOGIN failed.",
            "b'LOGIN failed.'",
            "[AUTHENTICATIONFAILED] LOGIN failed.",
            "AUTHENTICATIONFAILED",
        ],
    )
    def test_password_login_failed_is_not_retryable(self, text: str) -> None:
        """oauth=False (default, password path — UNCHANGED): a real wrong
        password must NOT be retried (it stays permanent)."""
        assert _is_retryable_imap_error(imaplib.IMAP4.error(text), oauth=False) is False
        # And the default (no kwarg) is the password path.
        assert _is_retryable_imap_error(imaplib.IMAP4.error(text)) is False

    @pytest.mark.parametrize(
        "text",
        [
            "User is authenticated but not connected",
            "not connected",
            "NO try again later",
            "service temporarily unavailable",
            "Too many simultaneous connections",
        ],
    )
    def test_existing_transient_substrings_still_retryable_for_oauth(self, text: str) -> None:
        """The base ADR-0026 transient family is preserved on the oauth path
        (the oauth set is a SUPERSET of the base set)."""
        assert _is_retryable_imap_error(imaplib.IMAP4.error(text), oauth=True) is True
        assert _is_retryable_imap_error(imaplib.IMAP4.error(text), oauth=False) is True

    @pytest.mark.parametrize(
        "text",
        [
            "NO invalid credentials",
            "account is disabled",
            "account has been blocked",
        ],
    )
    def test_real_permanent_markers_not_retryable_on_either_path(self, text: str) -> None:
        """disabled / blocked / invalid-credentials stay permanent for BOTH
        oauth and password (ADR-0028 §1 carve-out)."""
        assert _is_retryable_imap_error(imaplib.IMAP4.error(text), oauth=True) is False
        assert _is_retryable_imap_error(imaplib.IMAP4.error(text), oauth=False) is False

    def test_disabled_wins_over_nothing_but_login_failed_does_retry_oauth(self) -> None:
        """A reply carrying BOTH a real-permanent marker (disabled) and the
        flake substring is NOT retried even on oauth — permanent set is checked
        first."""
        exc = imaplib.IMAP4.error("account is disabled; LOGIN failed.")
        assert _is_retryable_imap_error(exc, oauth=True) is False

    def test_imap_abort_login_failed_retryable_oauth(self) -> None:
        assert _is_retryable_imap_error(imaplib.IMAP4.abort("LOGIN failed."), oauth=True) is True

    @pytest.mark.parametrize(
        "exc",
        [
            ValueError("LOGIN failed."),  # right text, wrong type
            RuntimeError("authenticationfailed"),
        ],
    )
    def test_non_imap_exception_not_retryable_even_oauth(self, exc: BaseException) -> None:
        assert _is_retryable_imap_error(exc, oauth=True) is False


# ---------------------------------------------------------------------------
# Scope B — _connect_and_login retry + kill-switch gating
# ---------------------------------------------------------------------------


class TestConnectLoginOAuthLoginFailedRetry:
    def test_oauth_login_failed_then_success_returns_mailbox(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """XOAUTH2 path, flag ON: "LOGIN failed." on attempt 1, success on
        attempt 2 -> returns the mailbox (exactly 2 attempts)."""
        _set_settings(monkeypatch, retries=3, kill_switch=True)
        flake = imaplib.IMAP4.error("b'LOGIN failed.'")
        box = _install_mailbox(monkeypatch, [flake, None])
        result = _connect_oauth()
        assert result is box
        assert box.login_calls == 2

    def test_oauth_persistent_login_failed_raises_after_retries_plus_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Flag ON: a persistent oauth "LOGIN failed." exhausts the 3 retries
        and re-raises after 1 + 3 = 4 attempts."""
        _set_settings(monkeypatch, retries=3, kill_switch=True)
        flake = imaplib.IMAP4.error("LOGIN failed.")
        box = _install_mailbox(monkeypatch, [flake, flake, flake, flake, flake])
        with pytest.raises(imaplib.IMAP4.error):
            _connect_oauth()
        assert box.login_calls == 4

    def test_kill_switch_off_oauth_login_failed_not_retried(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SYNC_OAUTH_LOGIN_FAILED_TRANSIENT=False disables the oauth gate: the
        XOAUTH2 path then treats "LOGIN failed." as permanent (no retry,
        propagates on the FIRST attempt) — the documented revert."""
        _set_settings(monkeypatch, retries=3, kill_switch=False)
        flake = imaplib.IMAP4.error("b'LOGIN failed.'")
        box = _install_mailbox(monkeypatch, [flake, None])
        with pytest.raises(imaplib.IMAP4.error):
            _connect_oauth()
        assert box.login_calls == 1  # gate off => no retry

    def test_password_login_failed_not_retried(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """REGRESSION: the password path (access_token None) never retries a
        "LOGIN failed." regardless of the flag — a wrong password is permanent."""
        _set_settings(monkeypatch, retries=3, kill_switch=True)
        flake = imaplib.IMAP4.error("b'LOGIN failed.'")
        box = _install_mailbox(monkeypatch, [flake, None])
        with pytest.raises(imaplib.IMAP4.error):
            _connect_password()
        assert box.login_calls == 1  # password path => no login-failed retry

    def test_oauth_disabled_account_not_retried(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Even on oauth with the flag on, a genuinely disabled mailbox is
        permanent and propagates on the first attempt."""
        _set_settings(monkeypatch, retries=3, kill_switch=True)
        box = _install_mailbox(monkeypatch, [imaplib.IMAP4.error("NO account is disabled"), None])
        with pytest.raises(imaplib.IMAP4.error):
            _connect_oauth()
        assert box.login_calls == 1

    def test_oauth_login_failed_logs_out_half_open_socket(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Each failed attempt best-effort closes its mailbox before retrying
        (one logout per failed attempt)."""
        _set_settings(monkeypatch, retries=3, kill_switch=True)
        flake = imaplib.IMAP4.error("LOGIN failed.")
        box = _install_mailbox(monkeypatch, [flake, None])
        _connect_oauth()
        assert box.logout_calls == 1
