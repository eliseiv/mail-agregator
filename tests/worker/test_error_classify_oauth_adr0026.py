"""ADR-0026 §1 rule-7 OAuth regression guards (worker/app/error_classify.py).

Scope A supplement. Pure unit tests (no I/O). These pin the MINOR-fix behaviour
the QA task calls out explicitly:

* ``oauth_token_error: oauth_exchange_failed`` — the ``OAuthError.code`` the token
  client raises for ANY non-200 Microsoft response that is NOT ``invalid_grant``
  (real provider 5xx / 429 / non-JSON on the refresh path). It MUST classify
  TRANSIENT with prefix ``oauth_token_error`` (WARNING, NOT rule-10 fail-open ->
  ``error`` + ERROR log), so a provider 5xx is never treated as our own bug and
  never disables a mailbox.
* Adding the ``oauth_exchange_failed`` substring to rule 7 MUST NOT swallow
  ``oauth_token_error: invalid_grant`` — that text stays PERMANENT (rule 8),
  prefix ``auth_failed``, ``is_explicit_permanent`` True. This is the regression
  the QA task guards against (adding a substring to rule 7 must not break it).
* ``oauth_token_unexpected: <Type>`` (the worker's catch-all wrap for a network /
  unexpected token error) — TRANSIENT, prefix ``oauth_token_error``.
"""

from __future__ import annotations

import pytest

from worker.app.error_classify import classify, error_prefix, is_explicit_permanent


class TestOAuthExchangeFailedTransient:
    """``oauth_exchange_failed`` => transient / oauth_token_error (provider 5xx)."""

    def test_oauth_exchange_failed_is_transient(self) -> None:
        assert classify("oauth_token_error: oauth_exchange_failed") == "transient"

    def test_oauth_exchange_failed_prefix_is_oauth_token_error(self) -> None:
        # MUST be oauth_token_error (WARNING path), NOT "error" (rule-10 fail-open,
        # which would log ERROR+traceback as if it were our bug).
        assert error_prefix("oauth_token_error: oauth_exchange_failed") == "oauth_token_error"

    def test_oauth_exchange_failed_not_explicit_permanent(self) -> None:
        assert is_explicit_permanent("oauth_token_error: oauth_exchange_failed") is False

    def test_oauth_unexpected_wrap_is_transient(self) -> None:
        # The worker's _resolve_oauth_access_token catch-all wraps unexpected
        # token errors as "oauth_token_unexpected: <Type>" -> rule 7 transient.
        # Use a type name with NO earlier-rule substring (e.g. avoid "timeout"
        # / "connection" / "ssl") so we exercise the rule-7 oauth prefix, not a
        # higher-priority first-match. ``RemoteProtocolError`` is httpx's wrap
        # for an unexpected provider response on the token endpoint.
        text = "oauth_token_unexpected: RemoteProtocolError"
        assert classify(text) == "transient"
        assert error_prefix(text) == "oauth_token_error"


class TestInvalidGrantStillPermanent:
    """Regression: oauth_exchange_failed substring must not break invalid_grant."""

    def test_invalid_grant_still_permanent(self) -> None:
        assert classify("oauth_token_error: invalid_grant") == "permanent"

    def test_invalid_grant_prefix_auth_failed(self) -> None:
        assert error_prefix("oauth_token_error: invalid_grant") == "auth_failed"

    def test_invalid_grant_is_explicit_permanent(self) -> None:
        assert is_explicit_permanent("oauth_token_error: invalid_grant") is True


@pytest.mark.parametrize(
    ("text", "expected_class", "expected_prefix", "explicit"),
    [
        ("oauth_token_error: oauth_exchange_failed", "transient", "oauth_token_error", False),
        ("oauth_token_error: token_5xx", "transient", "oauth_token_error", False),
        ("oauth_token_error: 429", "transient", "oauth_token_error", False),
        ("oauth_token_unexpected: ConnectError", "transient", "oauth_token_error", False),
        ("oauth_token_error: invalid_grant", "permanent", "auth_failed", True),
    ],
)
def test_oauth_table(text: str, expected_class: str, expected_prefix: str, explicit: bool) -> None:
    assert classify(text) == expected_class
    assert error_prefix(text) == expected_prefix
    assert is_explicit_permanent(text) is explicit
