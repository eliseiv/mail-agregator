"""ADR-0026 update §1 rule 3b — sporadic IMAP connection-state flake classify.

Scope C of the QA task for the ADR-0026 *update*. The new rule 3b in
``worker/app/error_classify.py`` classifies the sporadic Microsoft Outlook IMAP
"User is authenticated but not connected" / "not connected" flake as
TRANSIENT with the ``network`` UI prefix (a KNOWN transient -> WARNING), and it
must be evaluated BEFORE the permanent auth block so it never falls through to
rule 8 (auth_failed / permanent) or rule 10 (fail-open ``error``).

Regression guard: rule 3b must NOT change the existing contract — a real
``AUTHENTICATIONFAILED`` (which contains the substring "authenticated" but NOT
"authenticationfailed"... it DOES contain "authenticationfailed") still
classifies permanent/auth_failed and stays an explicit-permanent (instant
disable).

PURE unit scope: text + exception instances only, no I/O.
"""

from __future__ import annotations

import imaplib

import pytest

from worker.app.error_classify import (
    classify,
    error_prefix,
    is_explicit_permanent,
)

_FLAKE_TEXTS = [
    "User is authenticated but not connected",
    "user is authenticated but not connected (Failure)",
    "b'[ALERT] not connected'",
    "NO not connected, please retry",
]


# ---------------------------------------------------------------------------
# Rule 3b — the flake is a KNOWN transient with the ``network`` prefix
# ---------------------------------------------------------------------------


class TestImapConnFlakeRule3b:
    @pytest.mark.parametrize("text", _FLAKE_TEXTS)
    def test_flake_text_is_transient(self, text: str) -> None:
        assert classify(text) == "transient"

    @pytest.mark.parametrize("text", _FLAKE_TEXTS)
    def test_flake_prefix_is_network(self, text: str) -> None:
        """A known transient -> ``network`` prefix -> WARNING (NOT the rule-10
        fail-open ``error`` which would log ERROR + traceback as our own bug)."""
        assert error_prefix(text) == "network"

    def test_flake_imap4_error_instance_is_transient_network(self) -> None:
        exc = imaplib.IMAP4.error("User is authenticated but not connected")
        assert classify(exc) == "transient"
        assert error_prefix(exc) == "network"

    def test_flake_is_not_explicit_permanent(self) -> None:
        """The flake must never be an instant-disable permanent."""
        assert is_explicit_permanent("User is authenticated but not connected") is False

    def test_flake_with_authenticated_word_not_misrouted_to_auth(self) -> None:
        """ "authenticated but not connected" contains the word "authenticated"
        but NOT the permanent marker "authenticationfailed", and rule 3b (a
        transient rule, evaluated before the permanent block) wins anyway."""
        text = "User is authenticated but not connected"
        assert classify(text) == "transient"
        assert error_prefix(text) != "auth_failed"


# ---------------------------------------------------------------------------
# Regression — rule 3b did NOT break the permanent auth contract
# ---------------------------------------------------------------------------


class TestPermanentAuthStillBroken:
    def test_authenticationfailed_still_permanent_auth_failed(self) -> None:
        text = "b'[AUTHENTICATIONFAILED] Invalid credentials (Failure)'"
        assert classify(text) == "permanent"
        assert error_prefix(text) == "auth_failed"
        assert is_explicit_permanent(text) is True

    def test_authenticationfailed_imap4_error_instance_still_permanent(self) -> None:
        exc = imaplib.IMAP4.error("[AUTHENTICATIONFAILED] Invalid credentials")
        assert classify(exc) == "permanent"
        assert error_prefix(exc) == "auth_failed"
        assert is_explicit_permanent(exc) is True

    def test_authfailed_text_that_also_says_not_connected_is_transient(self) -> None:
        """Documents the first-match-wins ordering at the CLASSIFIER level: a
        reply that carries BOTH an auth marker and the rule-3b "not connected"
        flake substring is classified TRANSIENT (the whole transient block is
        evaluated before the permanent block). This differs from
        :func:`_is_retryable_imap_error`, which checks PERMANENT first — the two
        layers are intentionally separate (the retry layer must be conservative;
        the classifier is fail-open transient)."""
        text = "[AUTHENTICATIONFAILED] socket not connected"
        assert classify(text) == "transient"
        assert is_explicit_permanent(text) is False
