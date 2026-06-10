"""Unified error classification for the worker sync cycle (ADR-0026 sec. 1).

Single source of truth (in code) for the classification contract described in
``docs/adr/ADR-0026-sync-error-resilience.md`` sec. 1 and mirrored bit-for-bit
in ``docs/05-modules.md`` sec. 14.

Two public functions read **one** substring table (lower-case match) so the
UI prefix (what we show the user) and the class (what we do with the failure
counter) are computed from the same rules and never diverge -- this is the
architectural fix for root cause B (an ``auth_failed``-looking message such as
"too many simultaneous connections" must still be classified ``transient``).

Order of evaluation is strict top-to-bottom; the **first match wins**. The
transient block (rules 1-7, incl. rule 3b -- sporadic IMAP "authenticated but
not connected" / "not connected" flake, ADR-0026 update -- and rule 7b -- the
OAuth IMAP "login failed" flake, ADR-0028) is checked **entirely before** the
permanent block (rules 8-9): any transient marker beats any auth/permanent
marker in the same text. Rule 10 (unrecognised, incl. programming errors) is
fail-open -> transient.

Context (ADR-0028): the three public functions take a keyword-only
``auth_type: str | None = None``. The default reproduces the legacy behaviour
for every existing call; ``"oauth_outlook"`` activates rule 7b (OAuth IMAP auth
flake -> transient / ``network``). The classifier stays a pure function of
(text, exc, auth_type) -- the kill-switch
(``SYNC_OAUTH_LOGIN_FAILED_TRANSIENT``) is applied by the caller, which passes
``auth_type=None`` when the flag is off.

Usage::

    cls = classify(exc, auth_type=account.auth_type)   # "transient" | "permanent"
    prefix = error_prefix(exc, auth_type=account.auth_type)
"""

from __future__ import annotations

import asyncio
import errno
import socket
import ssl
from typing import Literal

from cryptography.exceptions import InvalidTag

ErrorClass = Literal["transient", "permanent"]

# Rule 1 / 5 / 9 instance tuples (module-level so isinstance stays a tuple call
# without tripping UP038's "use X | Y" -- a runtime tuple is required because
# these mix stdlib + third-party types and we test membership).
_TIMEOUT_TYPES: tuple[type[BaseException], ...] = (
    socket.timeout,
    TimeoutError,
    asyncio.TimeoutError,
)
_NETWORK_TYPES: tuple[type[BaseException], ...] = (ConnectionError, ssl.SSLError)
_DECRYPT_TYPES: tuple[type[BaseException], ...] = (InvalidTag, AssertionError)
# Union of all transient instance types EXCEPT the errno-gated ``OSError``
# (rule 6 needs the extra errno check). Merged into one isinstance call so the
# "any transient rule" predicate stays a single membership test (SIM101). Rule
# order is irrelevant for instance checks here because the predicate returns a
# single boolean OR -- a transient instance match wins over the permanent block
# either way (the permanent block is only consulted when this returns False).
_TRANSIENT_INSTANCE_TYPES: tuple[type[BaseException], ...] = (
    *_TIMEOUT_TYPES,
    socket.gaierror,
    *_NETWORK_TYPES,
)

# --- substring sets (lower-case), one shared table per ADR-0026 sec. 1 -------

# Rule 2 -- DNS / name-resolution failures.
_RESOLVE_SUBSTRINGS: tuple[str, ...] = (
    "could not resolve",
    "name or service not known",
    "temporary failure in name resolution",
    "nodename nor servname",
)

# Rule 3 -- provider rate-limit / "too many connections" / temporary
# unavailability. These win over auth markers (root cause B): a "LOGIN NO too
# many simultaneous connections" must be transient, never permanent.
_RATE_LIMIT_SUBSTRINGS: tuple[str, ...] = (
    "too many",
    "simultaneous",
    "try again",
    "temporarily",
    "unavailable",
    "inuse",
    "system error",
    "rate",
    "throttl",
)

# Rule 3b -- sporadic transient IMAP connection-state flakes (ADR-0026 update).
# Microsoft personal Outlook IMAP intermittently answers a valid XOAUTH2 on a
# HEALTHY mailbox with "User is authenticated but not connected"; it is a
# server-side flake, not an auth failure. Classified transient with the
# ``network`` UI-prefix (a KNOWN transient -> WARNING), never rule-10 fail-open
# (ERROR sync_account_unexpected_error). Checked BEFORE the permanent auth
# block. NOTE: "authenticated but not connected" contains "authenticated" but
# NOT "authenticationfailed", so it never matches rule 8 -- and being a
# transient rule it would win anyway (transient block is evaluated in full
# first).
_IMAP_CONN_FLAKE_SUBSTRINGS: tuple[str, ...] = (
    "authenticated but not connected",
    "not connected",
)

# Rule 4 -- generic timeout text (the isinstance timeout check is rule 1).
_TIMEOUT_SUBSTRINGS: tuple[str, ...] = (
    "timed out",
    "timeout",
)

# Rule 5 -- network / TLS substrings.
_NETWORK_SUBSTRINGS: tuple[str, ...] = (
    "connection refused",
    "connection reset",
    "broken pipe",
    "network is unreachable",
    "no route to host",
    "ssl",
)

# Rule 6 -- networking ``OSError`` errnos that mean a connect/transport failure.
_NETWORK_ERRNOS: frozenset[int] = frozenset(
    {
        errno.ECONNREFUSED,
        errno.ECONNRESET,
        errno.ETIMEDOUT,
        errno.EHOSTUNREACH,
        errno.ENETUNREACH,
        errno.EPIPE,
    }
)

# Rule 8 -- explicit permanent auth / account-state markers.
_AUTH_SUBSTRINGS: tuple[str, ...] = (
    "authenticationfailed",
    "invalid credentials",
    "login failed",
    "[alert]",
    "account is disabled",
    "account has been blocked",
)

# Rule 7b (ADR-0028) -- OAuth IMAP auth flakes. For ``auth_type='oauth_outlook'``
# accounts these substrings are TRANSIENT, not permanent: the XOAUTH2 access
# token was obtained via a SUCCESSFUL refresh BEFORE IMAP (sync_cycle
# ``get_valid_access_token``), so a "login failed" / "authenticationfailed"
# arriving at the IMAP step is a Microsoft server flake on a valid token, never
# a real auth failure (a real OAuth failure surfaces as ``invalid_grant`` on the
# refresh endpoint -> needs-consent, IMAP is never reached). A literal set: if
# Microsoft surfaces other wordings on prod, extend it here (ADR-0028
# Consequences). ``invalid_grant`` is deliberately excluded (it stays permanent,
# rule 8) -- see ``_matches_transient``.
_OAUTH_IMAP_AUTH_FLAKE_SUBSTRINGS: tuple[str, ...] = (
    "login failed",
    "authenticationfailed",
)

# Auth markers used by rule 3 to pick the UI prefix (auth_failed vs network)
# WITHOUT changing the transient class. A subset focused on "this text smells
# like a login response".
_AUTH_MARKER_SUBSTRINGS: tuple[str, ...] = (
    "auth",
    "login",
    "credential",
    "password",
)

# Rule 7 -- transient OAuth token-error codes (substring match on the text the
# worker already wraps OAuth failures into, e.g. "oauth_token_error: token_5xx").
#
# ``oauth_exchange_failed`` is the ``OAuthError.code`` the token client raises
# for ANY non-200 Microsoft response that is NOT ``invalid_grant`` (incl. real
# provider 5xx / 429 / non-JSON errors -- backend/app/oauth/service.py
# ``_TokenClient._post``). On the worker refresh path that is an EXPECTED
# provider-side transient, so it must classify ``transient`` with the
# ``oauth_token_error`` prefix (WARNING) rather than fall through to rule 10
# fail-open (which would log ERROR+traceback as if it were our own bug).
# ``invalid_grant`` is permanent: it never reaches the classifier as an
# ``OAuthError`` (the service raises ``OAuthRefreshInvalidError`` -> clean
# skip), and if its text ever appears it is matched by rule 8 -- so we
# deliberately do NOT add the bare ``oauth_token_error`` prefix marker here
# (that would also swallow ``oauth_token_error: invalid_grant`` into rule 7).
_OAUTH_TRANSIENT_SUBSTRINGS: tuple[str, ...] = (
    "5xx",
    "429",
    "token_network",
    "network",
    "timeout",
    "unexpected",
    "oauth_exchange_failed",
)


def _normalise(exc_or_text: object) -> tuple[str, BaseException | None]:
    """Return ``(lower_case_text, exc_or_None)``.

    For an exception we build ``"{type}: {exc}"`` lower-cased (per ADR-0026
    sec. 1) and keep the instance for ``isinstance`` checks. For a plain string
    we lower-case it and have no instance.
    """
    if isinstance(exc_or_text, BaseException):
        return f"{type(exc_or_text).__name__}: {exc_or_text}".lower(), exc_or_text
    return str(exc_or_text).lower(), None


def _has_any(text: str, needles: tuple[str, ...]) -> bool:
    return any(n in text for n in needles)


def _is_oauth_transient(exc: BaseException | None, text: str) -> bool:
    """Rule 7 -- OAuth httpx 5xx / 429 / network error.

    We avoid importing ``backend.app.oauth.service`` at module load (worker
    import-order); the worker wraps these into the ``oauth_token_error`` text it
    passes through. ``invalid_grant`` is permanent (rule 8) and handled there;
    here only the transient OAuth codes count.
    """
    del exc  # classification is text-based for OAuth (rule 7).
    if "oauth_token_error" not in text and "oauth_token_unexpected" not in text:
        return False
    return _has_any(text, _OAUTH_TRANSIENT_SUBSTRINGS)


def _is_oauth_imap_auth_flake(text: str, auth_type: str | None) -> bool:
    """Rule 7b (ADR-0028) -- sporadic Microsoft IMAP auth flake on OAuth.

    True only for ``auth_type == "oauth_outlook"`` when the text carries a
    flake substring (``login failed`` / ``authenticationfailed``) AND does NOT
    carry ``invalid_grant`` (a real refresh failure, which stays permanent via
    rule 8). For ``password`` / ``None`` this is always False, so the legacy
    rule-8 permanent path is unchanged.
    """
    if auth_type != "oauth_outlook":
        return False
    if "invalid_grant" in text:
        return False
    return _has_any(text, _OAUTH_IMAP_AUTH_FLAKE_SUBSTRINGS)


def _matches_transient(
    exc: BaseException | None, text: str, *, auth_type: str | None = None
) -> bool:
    """True if any transient rule (1-7, incl. 7b) matches.

    First-match-wins is preserved because the permanent block is only consulted
    when this returns False. Rule 7b (ADR-0028) is evaluated as part of the
    transient block, i.e. BEFORE the permanent rule 8: for an ``oauth_outlook``
    account an IMAP "login failed" is caught here (transient) and never reaches
    rule 8 (permanent).
    """
    return (
        # Rules 1 / 2 / 5 -- timeout / gaierror / connection-TLS instances.
        isinstance(exc, _TRANSIENT_INSTANCE_TYPES)
        # Rule 2 -- DNS / resolution failures (text).
        or _has_any(text, _RESOLVE_SUBSTRINGS)
        # Rule 3 -- provider rate-limit / temporary unavailability (beats auth).
        or _has_any(text, _RATE_LIMIT_SUBSTRINGS)
        # Rule 3b -- sporadic transient IMAP connection-state flake.
        or _has_any(text, _IMAP_CONN_FLAKE_SUBSTRINGS)
        # Rule 4 -- generic timeout text.
        or _has_any(text, _TIMEOUT_SUBSTRINGS)
        # Rule 5 -- connection / TLS errors (text).
        or _has_any(text, _NETWORK_SUBSTRINGS)
        # Rule 6 -- networking OSError with a transport errno.
        or (isinstance(exc, OSError) and exc.errno in _NETWORK_ERRNOS)
        # Rule 7 -- transient OAuth token errors.
        or _is_oauth_transient(exc, text)
        # Rule 7b -- OAuth IMAP auth flake (login failed / authenticationfailed).
        or _is_oauth_imap_auth_flake(text, auth_type)
    )


def _matches_permanent(
    exc: BaseException | None, text: str, *, auth_type: str | None = None
) -> bool:
    """True if a permanent rule (8-9) matches.

    Only consulted after the whole transient block (1-7, incl. 7b) failed. The
    ``auth_type`` parameter is accepted for signature symmetry with
    :func:`_matches_transient`; rule 8/9 markers are auth-type-independent (the
    OAuth carve-out happens earlier in the transient block via rule 7b).
    """
    del auth_type  # rule 8/9 are context-independent; carve-out is rule 7b.
    return (
        # Rule 8 -- explicit auth / account-state failures + oauth invalid_grant.
        _has_any(text, _AUTH_SUBSTRINGS)
        or "invalid_grant" in text
        # Rule 9 -- decrypt failure (wrong key / tampered ciphertext).
        or isinstance(exc, _DECRYPT_TYPES)
        or "invalidtag" in text
        or "decrypt_fail" in text
    )


def classify(exc_or_text: object, *, auth_type: str | None = None) -> ErrorClass:
    """Classify an exception or error text as ``"transient"`` or ``"permanent"``.

    Contract: ADR-0026 sec. 1 table (+ ADR-0028 rule 7b). Transient block
    (rules 1-7, incl. 7b) is evaluated in full before the permanent block
    (rules 8-9); rule 10 (anything else, including programming errors) is
    fail-open -> ``"transient"``.

    ``auth_type`` (keyword-only, default ``None`` = legacy behaviour) enables
    the ADR-0028 context branch: for ``"oauth_outlook"`` an IMAP "login failed"
    / "authenticationfailed" (without ``invalid_grant``) is TRANSIENT (rule 7b).
    """
    text, exc = _normalise(exc_or_text)
    if _matches_transient(exc, text, auth_type=auth_type):
        return "transient"
    if _matches_permanent(exc, text, auth_type=auth_type):
        return "permanent"
    # Rule 10 -- fail-open.
    return "transient"


def error_prefix(exc_or_text: object, *, auth_type: str | None = None) -> str:  # noqa: PLR0911 -- one return per rule (ADR-0026 sec. 1 table)
    """Compute the UI prefix for ``last_sync_error`` from the SAME table.

    Returns one of: ``invalid_host`` | ``auth_failed`` | ``timeout`` |
    ``network`` | ``oauth_token_error`` | ``decrypt_fail`` | ``error``.

    ``auth_type`` (keyword-only, default ``None`` = legacy behaviour) selects
    the ADR-0028 rule 7b branch: an OAuth IMAP auth flake returns ``network``
    (a transient, not the scary ``auth_failed``), checked BEFORE rule 8.
    """
    text, exc = _normalise(exc_or_text)

    # Rule 1 -- timeout instances.
    if isinstance(exc, _TIMEOUT_TYPES):
        return "timeout"
    # Rule 2 -- DNS / resolution failures.
    if isinstance(exc, socket.gaierror) or _has_any(text, _RESOLVE_SUBSTRINGS):
        return "invalid_host"
    # Rule 3 -- provider rate-limit: auth_failed if the text also smells like an
    # auth response, else network. Class stays transient regardless.
    if _has_any(text, _RATE_LIMIT_SUBSTRINGS):
        return "auth_failed" if _has_any(text, _AUTH_MARKER_SUBSTRINGS) else "network"
    # Rule 3b -- sporadic transient IMAP connection-state flake -> network.
    if _has_any(text, _IMAP_CONN_FLAKE_SUBSTRINGS):
        return "network"
    # Rule 4 -- generic timeout text.
    if _has_any(text, _TIMEOUT_SUBSTRINGS):
        return "timeout"
    # Rule 5 -- connection / TLS errors.
    if isinstance(exc, _NETWORK_TYPES) or _has_any(text, _NETWORK_SUBSTRINGS):
        return "network"
    # Rule 6 -- networking OSError with a transport errno.
    if isinstance(exc, OSError) and exc.errno in _NETWORK_ERRNOS:
        return "network"
    # Rule 7 -- transient OAuth token errors.
    if _is_oauth_transient(exc, text):
        return "oauth_token_error"
    # Rule 7b (ADR-0028) -- OAuth IMAP auth flake -> network (transient prefix),
    # checked BEFORE rule 8 so an oauth_outlook "login failed" is not surfaced
    # as auth_failed. password / None never reach this (auth_type guard).
    if _is_oauth_imap_auth_flake(text, auth_type):
        return "network"
    # Rule 8 -- explicit auth / account-state failures.
    if _has_any(text, _AUTH_SUBSTRINGS) or "invalid_grant" in text:
        return "auth_failed"
    # Rule 9 -- decrypt failure.
    if isinstance(exc, _DECRYPT_TYPES) or "invalidtag" in text or "decrypt_fail" in text:
        return "decrypt_fail"
    # Rule 10 -- unrecognised.
    return "error"


def is_explicit_permanent(exc_or_text: object, *, auth_type: str | None = None) -> bool:
    """True for rule 8 (auth) / rule 9 (decrypt) -- the "instant disable"
    permanents (no consecutive-failures threshold needed, ADR-0026 sec. 2/3).

    Only meaningful when :func:`classify` already returned ``"permanent"``.

    ``auth_type`` (keyword-only, default ``None`` = legacy behaviour) feeds the
    same transient guard: for ``"oauth_outlook"`` an IMAP auth flake matches
    rule 7b (transient), so this returns ``False`` -- the OAuth flake can never
    trigger an instant disable (ADR-0028 sec. 1).
    """
    text, exc = _normalise(exc_or_text)
    # An explicit permanent must not be shadowed by a transient rule.
    if _matches_transient(exc, text, auth_type=auth_type):
        return False
    return _matches_permanent(exc, text, auth_type=auth_type)
