"""Telegram WebApp ``initData`` HMAC validator (ADR-0022 §1.2).

Pure function: no I/O, no DB, no side effects. The ``init_data`` string is
the verbatim value of ``window.Telegram.WebApp.initData`` (URL-encoded
key=value pairs joined by ``&``); we validate the HMAC-SHA256 signature
using the bot token as documented at
https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app

This module **never logs the raw initData** — it can contain a freshly
issued auth token and PII (Telegram user first_name / username). Failures
are returned as ``None`` + an error type for the caller to log a redacted
summary.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Literal
from urllib.parse import parse_qsl

InitDataError = Literal[
    "malformed",
    "missing_hash",
    "missing_user",
    "invalid_user_payload",
    "missing_auth_date",
    "hash_mismatch",
    "expired",
]


@dataclass(frozen=True, slots=True)
class ValidatedInitData:
    """Successful validation outcome.

    Only fields actually consumed by the SSO flow are exposed. We
    deliberately don't surface raw key/value pairs to keep callers from
    accidentally trusting unsigned data.
    """

    telegram_user_id: int
    first_name: str | None
    username: str | None
    auth_date: int  # unix seconds


def _build_data_check_string(pairs: list[tuple[str, str]]) -> str:
    """Telegram's canonical ``data_check_string``: ``key=value`` pairs
    sorted by key, joined by newline, ``hash`` excluded.
    """
    filtered = [(k, v) for k, v in pairs if k != "hash"]
    filtered.sort(key=lambda kv: kv[0])
    return "\n".join(f"{k}={v}" for k, v in filtered)


def _secret_key(bot_token: str) -> bytes:
    """``HMAC_SHA256(key='WebAppData', msg=bot_token)`` — per Telegram spec."""
    return hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()


def verify_init_data(  # noqa: PLR0911 - each return is one named failure mode of HMAC validation
    raw: str, *, bot_token: str, max_age_seconds: int, now: int | None = None
) -> ValidatedInitData | InitDataError:
    """Validate ``raw`` initData with ``bot_token``.

    Returns a :class:`ValidatedInitData` on success, or one of the
    :data:`InitDataError` literals on failure. The function does not raise:
    callers map the literal to a 401 ``invalid_init_data`` /
    ``init_data_expired`` envelope per ``docs/04-api-contracts.md``.

    ``now`` is injectable for tests; production callers leave it None
    and we use :func:`time.time`.
    """
    if not raw or not bot_token:
        return "malformed"

    # ``parse_qsl(keep_blank_values=True, strict_parsing=False)``: tolerate
    # empty values (``photo_url=``) but reject malformed pairs. Telegram's
    # spec keeps order in the wire format but we sort below; ``parse_qsl``
    # preserves duplicates as separate tuples — Telegram never duplicates
    # keys in initData, so a duplicate is an indicator of tampering.
    try:
        pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        return "malformed"
    if not pairs:
        return "malformed"

    keys_seen: set[str] = set()
    for k, _ in pairs:
        if k in keys_seen:
            return "malformed"
        keys_seen.add(k)

    submitted_hash: str | None = None
    user_field: str | None = None
    auth_date_field: str | None = None
    for k, v in pairs:
        if k == "hash":
            submitted_hash = v
        elif k == "user":
            user_field = v
        elif k == "auth_date":
            auth_date_field = v

    if submitted_hash is None or not submitted_hash:
        return "missing_hash"
    if user_field is None:
        return "missing_user"
    if auth_date_field is None:
        return "missing_auth_date"

    try:
        auth_date = int(auth_date_field)
    except ValueError:
        return "missing_auth_date"

    data_check_string = _build_data_check_string(pairs)
    computed = hmac.new(
        _secret_key(bot_token),
        data_check_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(computed, submitted_hash):
        return "hash_mismatch"

    # Reject the value submitted as ``"hash"`` only after HMAC has cleared
    # — otherwise a malformed payload could be probed for length-leak
    # before the constant-time compare. Order matters.
    current = int(now if now is not None else time.time())
    if current - auth_date > max_age_seconds:
        return "expired"

    try:
        user_payload = json.loads(user_field)
    except (json.JSONDecodeError, TypeError):
        return "invalid_user_payload"
    if not isinstance(user_payload, dict):
        return "invalid_user_payload"

    raw_id = user_payload.get("id")
    if not isinstance(raw_id, int):
        return "invalid_user_payload"

    first_name = user_payload.get("first_name")
    if first_name is not None and not isinstance(first_name, str):
        first_name = None
    username = user_payload.get("username")
    if username is not None and not isinstance(username, str):
        username = None

    return ValidatedInitData(
        telegram_user_id=int(raw_id),
        first_name=first_name,
        username=username,
        auth_date=auth_date,
    )
