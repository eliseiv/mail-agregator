"""Unit tests for ADR-0026 §4 IMAP connect/login retry (imap_fetcher).

Scope E. We mock the imap_tools mailbox (external boundary) so the retry loop
in ``_connect_and_login`` runs without a real network, and patch ``time.sleep``
so backoff does not actually block the test.

Contract under test:
* gaierror / ConnectionError / retryable OSError -> retried up to
  SYNC_CONNECT_RETRIES, then the final error propagates.
* socket.timeout / TimeoutError -> NOT retried (1 attempt only).
* auth/login failures -> NOT retried (surface immediately).
* success on a retry returns the mailbox.
"""

from __future__ import annotations

import errno
import socket
from typing import Any

import pytest

import worker.app.imap_fetcher as fetcher
from worker.app.imap_fetcher import _is_retryable_connect_error


class _FakeMailbox:
    """Minimal stand-in for imap_tools.BaseMailBox.

    ``login`` / ``xoauth2`` raise from a queue of exceptions; a ``None`` entry
    means "succeed". ``logout`` is a no-op counter.
    """

    def __init__(self, outcomes: list[BaseException | None]) -> None:
        self._outcomes = outcomes
        self.login_calls = 0
        self.logout_calls = 0

    def login(self, username: str, password: str, *, initial_folder: str = "INBOX") -> None:
        self._next()

    def xoauth2(self, username: str, token: str, *, initial_folder: str = "INBOX") -> None:
        self._next()

    def _next(self) -> None:
        self.login_calls += 1
        outcome = self._outcomes.pop(0)
        if outcome is not None:
            raise outcome

    def logout(self) -> None:
        self.logout_calls += 1


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never actually sleep during retry backoff (deterministic, fast)."""
    monkeypatch.setattr(fetcher.time, "sleep", lambda _s: None)


def _install_mailbox(
    monkeypatch: pytest.MonkeyPatch, outcomes: list[BaseException | None]
) -> _FakeMailbox:
    box = _FakeMailbox(outcomes)
    monkeypatch.setattr(fetcher, "_open_mailbox", lambda **_kw: box)
    return box


def _set_retries(monkeypatch: pytest.MonkeyPatch, n: int) -> None:
    """Force SYNC_CONNECT_RETRIES regardless of env."""

    class _S:
        SYNC_CONNECT_RETRIES = n

    monkeypatch.setattr(fetcher, "get_settings", lambda: _S())


def _connect(box_unused: Any = None) -> Any:
    return fetcher._connect_and_login(
        host="imap.example.com",
        port=993,
        ssl_on=True,
        username="u@example.com",
        password="pw",
        access_token=None,
        timeout=30,
    )


# --- _is_retryable_connect_error predicate ---------------------------------


class TestIsRetryablePredicate:
    def test_gaierror_retryable(self) -> None:
        assert _is_retryable_connect_error(socket.gaierror(-2, "name")) is True

    def test_connectionerror_retryable(self) -> None:
        assert _is_retryable_connect_error(ConnectionError("reset")) is True

    def test_oserror_econnrefused_retryable(self) -> None:
        assert _is_retryable_connect_error(OSError(errno.ECONNREFUSED, "refused")) is True

    def test_timeout_not_retryable(self) -> None:
        assert _is_retryable_connect_error(TimeoutError("timed out")) is False
        assert _is_retryable_connect_error(TimeoutError()) is False

    def test_oserror_non_network_errno_not_retryable(self) -> None:
        assert _is_retryable_connect_error(OSError(errno.ENOENT, "nope")) is False

    def test_generic_exception_not_retryable(self) -> None:
        assert _is_retryable_connect_error(ValueError("auth failed")) is False


# --- retry loop behaviour ---------------------------------------------------


class TestConnectRetryLoop:
    def test_gaierror_is_retried_then_propagates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _set_retries(monkeypatch, 2)
        err = socket.gaierror(-2, "Name or service not known")
        box = _install_mailbox(monkeypatch, [err, err, err])
        with pytest.raises(socket.gaierror):
            _connect()
        # 1 initial + 2 retries = 3 attempts.
        assert box.login_calls == 3

    def test_connectionerror_retried_then_succeeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _set_retries(monkeypatch, 2)
        box = _install_mailbox(monkeypatch, [ConnectionError("reset"), None])
        result = _connect()
        assert result is box
        assert box.login_calls == 2  # failed once, succeeded on retry

    def test_timeout_not_retried(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _set_retries(monkeypatch, 2)
        box = _install_mailbox(monkeypatch, [TimeoutError("timed out")])
        with pytest.raises(socket.timeout):
            _connect()
        assert box.login_calls == 1  # NO retry for timeout

    def test_auth_error_not_retried(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _set_retries(monkeypatch, 2)
        # imap_tools surfaces login failures as a generic exception (not a
        # network type) -> not retryable.
        box = _install_mailbox(
            monkeypatch, [RuntimeError("[AUTHENTICATIONFAILED] Invalid credentials")]
        )
        with pytest.raises(RuntimeError):
            _connect()
        assert box.login_calls == 1  # NO retry for auth

    def test_zero_retries_means_single_attempt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _set_retries(monkeypatch, 0)
        box = _install_mailbox(monkeypatch, [socket.gaierror(-2, "name")])
        with pytest.raises(socket.gaierror):
            _connect()
        assert box.login_calls == 1

    def test_success_first_try_no_retry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _set_retries(monkeypatch, 2)
        box = _install_mailbox(monkeypatch, [None])
        result = _connect()
        assert result is box
        assert box.login_calls == 1
