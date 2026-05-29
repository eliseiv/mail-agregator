"""Unit tests for ADR-0026 §1 error classification (worker/app/error_classify.py).

Scope A of the QA task. These are PURE unit tests (no I/O, no DB): they feed the
exact production-incident texts and exception instances through ``classify`` /
``error_prefix`` / ``is_explicit_permanent`` and assert the class + UI prefix.

Root cause B regression guard: a "too many simultaneous connections" reply that
ALSO carries the ``[ALERT]`` auth marker must classify TRANSIENT (rule 3 wins
over rule 8), so the worker never disables a mailbox during a provider
rate-limit / connection-cap storm.
"""

from __future__ import annotations

import errno
import socket
import ssl

import pytest
from cryptography.exceptions import InvalidTag

from worker.app.error_classify import (
    classify,
    error_prefix,
    is_explicit_permanent,
)

# ---------------------------------------------------------------------------
# A. Incident texts — classification (the critical table)
# ---------------------------------------------------------------------------


class TestClassifyIncidentTexts:
    def test_exact_incident_too_many_with_alert_is_transient(self) -> None:
        """Root cause B: the EXACT prod-incident text (rule 3 rate-limit) MUST
        beat rule 8 ([ALERT] auth). This is the text that disabled 81/85
        mailboxes before ADR-0026."""
        text = (
            'Response status "OK" expected, but "NO" received. '
            "Data: [b'[ALERT] Too many simultaneous connections. (Failure)']"
        )
        assert classify(text) == "transient"

    def test_too_many_simultaneous_connections_with_alert_is_transient(self) -> None:
        text = "[ALERT] Too many simultaneous connections. Please try again later."
        assert classify(text) == "transient"

    def test_too_many_connections_lower_no_alert_is_transient(self) -> None:
        text = "LOGIN NO too many simultaneous connections from this IP"
        assert classify(text) == "transient"

    def test_could_not_resolve_host_text_is_transient(self) -> None:
        assert classify("invalid_host: Could not resolve host") == "transient"

    def test_socket_gaierror_is_transient(self) -> None:
        exc = socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        assert classify(exc) == "transient"

    def test_authenticationfailed_invalid_credentials_is_permanent(self) -> None:
        text = "b'[AUTHENTICATIONFAILED] Invalid credentials (Failure)'"
        assert classify(text) == "permanent"

    def test_invalid_grant_is_permanent(self) -> None:
        assert classify("oauth_token_error: invalid_grant") == "permanent"

    def test_decrypt_invalidtag_is_permanent(self) -> None:
        assert classify(InvalidTag()) == "permanent"

    def test_decrypt_invalidtag_text_is_permanent(self) -> None:
        assert classify("cryptography.exceptions.InvalidTag") == "permanent"

    def test_cannot_select_inbox_with_auth_marker_is_permanent(self) -> None:
        """``cannot_select_inbox`` is classified by the GENERAL table (ADR-0026
        §1 / docs/05-modules.md §14): an account-state reply that carries an auth
        marker AND no transient marker -> permanent ``auth_failed``.

        NB: the fixture deliberately avoids transient words (e.g. "unavailable",
        which is a rule-3 rate-limit marker) — those would correctly win under
        first-match-wins and flip the class to transient."""
        text = "cannot_select_inbox: NO [AUTHENTICATIONFAILED] permission denied"
        assert classify(text) == "permanent"
        assert error_prefix(text) == "auth_failed"

    def test_cannot_select_inbox_with_transient_word_stays_transient(self) -> None:
        """Root-cause-B corollary: if the SELECT-failure reply also contains a
        transient marker (here "unavailable" -> rule 3), the transient block wins
        even over the [AUTHENTICATIONFAILED] auth marker. Documents that the word
        choice in the server reply matters and a transient marker is never
        shadowed by a later auth marker (ADR-0026 §1 first-match-wins)."""
        text = "cannot_select_inbox: [AUTHENTICATIONFAILED] mailbox unavailable"
        assert classify(text) == "transient"
        assert is_explicit_permanent(text) is False

    def test_cannot_select_inbox_bare_is_transient_by_failopen(self) -> None:
        """Contract note (docs/05-modules.md §14): a bare cannot_select_inbox /
        MailboxFolderSelectError reply with NO auth/transient marker matches no
        rule 1-9 -> rule-10 fail-open -> TRANSIENT. This is intentional (a false
        transient just retries; a false permanent would wrongly disable)."""
        text = "cannot_select_inbox: command SELECT INBOX failed"
        assert classify(text) == "transient"

    def test_timeout_text_is_transient(self) -> None:
        assert classify("connection timed out") == "transient"

    def test_timeout_instances_are_transient(self) -> None:
        # ``socket.timeout`` is an alias of ``TimeoutError`` on py3.10+.
        assert classify(TimeoutError("timed out")) == "transient"
        assert classify(TimeoutError()) == "transient"

    def test_timeout_prefix_is_timeout(self) -> None:
        # §4 note mirrored here: timeout is transient but the fetcher does NOT
        # retry it (see test_connect_retry::test_timeout_not_retried).
        assert error_prefix(TimeoutError("x")) == "timeout"
        assert classify(TimeoutError("x")) == "transient"

    def test_typeerror_unexpected_is_transient_failopen(self) -> None:
        """Rule 10: programming errors fail open to transient (never disable)."""
        assert classify(TypeError("argument of type 'int' is not iterable")) == "transient"

    def test_keyerror_unexpected_is_transient_failopen(self) -> None:
        assert classify(KeyError("missing")) == "transient"

    def test_connection_refused_oserror_is_transient(self) -> None:
        exc = OSError(errno.ECONNREFUSED, "Connection refused")
        assert classify(exc) == "transient"

    def test_ssl_error_is_transient(self) -> None:
        assert classify(ssl.SSLError("bad handshake")) == "transient"

    def test_oauth_5xx_is_transient(self) -> None:
        assert classify("oauth_token_error: token_5xx") == "transient"

    def test_oauth_429_is_transient(self) -> None:
        assert classify("oauth_token_error: 429") == "transient"


# ---------------------------------------------------------------------------
# A. error_prefix must stay consistent with the class (same table)
# ---------------------------------------------------------------------------


class TestErrorPrefixConsistency:
    def test_too_many_connections_with_auth_marker_prefix_auth_failed(self) -> None:
        """Rule 3: when the rate-limit text ALSO smells like a login response,
        the UI prefix is ``auth_failed`` but the CLASS stays transient."""
        text = "[ALERT] Too many simultaneous connections (login)"
        assert error_prefix(text) == "auth_failed"
        assert classify(text) == "transient"

    def test_too_many_connections_no_auth_marker_prefix_network(self) -> None:
        text = "too many simultaneous connections, try again"
        assert error_prefix(text) == "network"
        assert classify(text) == "transient"

    def test_could_not_resolve_prefix_invalid_host(self) -> None:
        assert error_prefix("Could not resolve host: x") == "invalid_host"
        assert classify("Could not resolve host: x") == "transient"

    def test_gaierror_prefix_invalid_host(self) -> None:
        exc = socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        assert error_prefix(exc) == "invalid_host"

    def test_authfailed_prefix_auth_failed(self) -> None:
        text = "[AUTHENTICATIONFAILED] Invalid credentials"
        assert error_prefix(text) == "auth_failed"
        assert classify(text) == "permanent"

    def test_invalid_grant_prefix_auth_failed(self) -> None:
        assert error_prefix("oauth_token_error: invalid_grant") == "auth_failed"

    def test_decrypt_prefix_decrypt_fail(self) -> None:
        assert error_prefix(InvalidTag()) == "decrypt_fail"
        assert error_prefix("decrypt_fail") == "decrypt_fail"

    def test_timeout_prefix_timeout(self) -> None:
        assert error_prefix(TimeoutError("timed out")) == "timeout"
        assert error_prefix("connection timed out") == "timeout"

    def test_network_prefix_network(self) -> None:
        assert error_prefix(ssl.SSLError("x")) == "network"
        assert error_prefix("connection refused") == "network"

    def test_oauth_prefix_oauth_token_error(self) -> None:
        assert error_prefix("oauth_token_error: token_5xx") == "oauth_token_error"

    def test_unrecognised_prefix_error(self) -> None:
        assert error_prefix(TypeError("boom")) == "error"
        assert classify(TypeError("boom")) == "transient"

    @pytest.mark.parametrize(
        ("inp", "expected_prefix", "expected_class"),
        [
            ("[ALERT] Too many simultaneous connections (login)", "auth_failed", "transient"),
            ("Could not resolve host", "invalid_host", "transient"),
            ("[AUTHENTICATIONFAILED] Invalid credentials", "auth_failed", "permanent"),
            ("oauth_token_error: invalid_grant", "auth_failed", "permanent"),
            ("oauth_token_error: token_5xx", "oauth_token_error", "transient"),
            ("connection timed out", "timeout", "transient"),
        ],
    )
    def test_prefix_class_pairs(self, inp: str, expected_prefix: str, expected_class: str) -> None:
        assert error_prefix(inp) == expected_prefix
        assert classify(inp) == expected_class


# ---------------------------------------------------------------------------
# A. is_explicit_permanent (instant-disable permanents) — ADR-0026 §2/§3
# ---------------------------------------------------------------------------


class TestIsExplicitPermanent:
    def test_authfailed_is_explicit(self) -> None:
        assert is_explicit_permanent("[AUTHENTICATIONFAILED] Invalid credentials") is True

    def test_invalid_grant_is_explicit(self) -> None:
        assert is_explicit_permanent("oauth_token_error: invalid_grant") is True

    def test_decrypt_is_explicit(self) -> None:
        assert is_explicit_permanent(InvalidTag()) is True

    def test_transient_is_not_explicit(self) -> None:
        # rate-limit / DNS / timeout must never be "instant disable".
        assert is_explicit_permanent("[ALERT] Too many simultaneous connections") is False
        assert is_explicit_permanent("Could not resolve host") is False
        assert is_explicit_permanent(TimeoutError("timed out")) is False

    def test_failopen_unexpected_is_not_explicit(self) -> None:
        assert is_explicit_permanent(TypeError("boom")) is False
