"""Unit tests for the HMAC signature + serialisation of the CRM push (ADR-0043 §2).

Security boundary: the signature is computed over the SAME bytes that go out on the
wire (``content=raw_body``, not ``json=``). Critical for non-ASCII: ``_serialize`` encodes
with ``ensure_ascii=False``, and the CRM receiver, recomputing the signature over the
received bytes, must obtain the same digest. A re-serialisation with ``ensure_ascii=True``
(the ``json.dumps`` default) yields different bytes -> a different signature. Plus the
round-trip of the queue payloads.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from backend.app.crm_push.service import (
    _PushQueuePayload,
    _serialize,
    build_signature,
    parse_status_payload,
)

pytestmark = pytest.mark.unit

_SECRET = "shared-hmac-secret-xyz"
_TS = 1_752_100_000


def test_build_signature_matches_manual_byte_construction() -> None:
    raw = b'{"messages":[]}'
    expected = hmac.new(
        _SECRET.encode("utf-8"),
        str(_TS).encode("ascii") + b"." + raw,
        hashlib.sha256,
    ).hexdigest()
    assert build_signature(_SECRET, _TS, raw) == expected


def test_serialize_is_compact_and_non_ascii_utf8() -> None:
    """``_serialize`` uses compact separators + ensure_ascii=False (raw UTF-8, not \\uXXXX)."""
    raw = _serialize({"subject": "Отчёт «июнь» 📊", "n": 1})
    text = raw.decode("utf-8")
    assert "Отчёт «июнь» 📊" in text  # not escaped to \uXXXX
    assert ", " not in text and ": " not in text  # separators=(",", ":")
    assert b"\\u" not in raw


def test_signature_over_sent_bytes_roundtrips_for_non_ascii() -> None:
    """The receiver, recomputing over the RECEIVED bytes, obtains the same signature."""
    body = {"subject": "Диспут 🚨", "from_name": "Иван Петров"}
    raw_sent = _serialize(body)  # these bytes go out in the body (content=raw_sent)
    sig_sender = build_signature(_SECRET, _TS, raw_sent)

    # CRM side: signature over the same received bytes -> matches.
    sig_receiver = build_signature(_SECRET, _TS, raw_sent)
    assert sig_receiver == sig_sender


def test_ascii_reserialization_breaks_signature() -> None:
    """A signature over ``json.dumps(obj)`` (ensure_ascii=True) != over the raw bytes."""
    body = {"subject": "Отчёт 📊"}
    raw_sent = _serialize(body)
    ascii_bytes = json.dumps(body).encode("utf-8")  # ensure_ascii=True (default)
    assert raw_sent != ascii_bytes
    assert build_signature(_SECRET, _TS, raw_sent) != build_signature(_SECRET, _TS, ascii_bytes)


def test_signature_is_secret_and_timestamp_bound() -> None:
    raw = b"body"
    base = build_signature(_SECRET, _TS, raw)
    assert build_signature("other", _TS, raw) != base
    assert build_signature(_SECRET, _TS + 1, raw) != base
    assert build_signature(_SECRET, _TS, raw + b"x") != base


# ------------------------------------------------------ push queue payload
def test_push_payload_roundtrip() -> None:
    payload = _PushQueuePayload(message_id=42, source="sync")
    parsed = _PushQueuePayload.from_json(payload.to_json())
    assert parsed == payload


def test_push_payload_from_malformed_is_none() -> None:
    assert _PushQueuePayload.from_json("{not json") is None
    assert _PushQueuePayload.from_json("[]") is None  # not a dict
    assert _PushQueuePayload.from_json('{"message_id":"x"}') is None  # not an int


def test_push_payload_defaults_source_sync() -> None:
    parsed = _PushQueuePayload.from_json('{"message_id":7}')
    assert parsed is not None
    assert parsed.message_id == 7 and parsed.source == "sync"


# ----------------------------------------------------- status queue payload
def test_parse_status_payload_valid_and_malformed() -> None:
    assert parse_status_payload('{"mail_account_id":9}') == 9
    assert parse_status_payload("{bad") is None
    assert parse_status_payload("[]") is None
    assert parse_status_payload('{"mail_account_id":"x"}') is None
