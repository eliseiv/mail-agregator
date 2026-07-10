"""Cross-repo HMAC vector: the aggregator's signature == the CRM's verification (ADR-0045 §3).

The headless-OAuth notification (``POST {CRM_OAUTH_INGEST_URL}``) is signed with the SAME
HMAC contract as ``/api/mail/ingest`` (ADR-0043 §2): the aggregator computes
``build_signature`` and the CRM verifies ``compute_mail_push_signature``. Both build
``mac_input`` byte-wise (``str(ts).encode("ascii") + b"." + raw_body``) and MUST agree on the
hex digest.

A FIXED vector (secret + timestamp + raw non-ASCII body with a Cyrillic ``display_name``) is
pinned here; the SAME constants are pinned in the CRM's paired test
(``backend/tests/unit/test_crm_oauth_hmac_vector.py``). If either side changes its signature
canon or body serialisation, the hex diverges and one of the two tests fails — detecting the
contract drift across separate runs / repos.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from backend.app.crm_push.service import build_signature

pytestmark = pytest.mark.unit

# --- PINNED CROSS-REPO VECTOR (identical to the CRM side) --------------------
_VECTOR_SECRET = "shared-oauth-hmac-secret-v1"
_VECTOR_TS = 1_752_500_000
_VECTOR_BODY = {
    "crm_state": "Zm9vLmJhcg",
    "mail_account_id": 7,
    "email": "box@outlook.com",
    "display_name": "Иван Пётр 📧",  # non-ASCII: Cyrillic + emoji
    "is_active": True,
}
_VECTOR_RAW_HEX = (
    "7b2263726d5f7374617465223a225a6d39764c6d4a686367222c226d61696c5f6163636f"
    "756e745f6964223a372c22656d61696c223a22626f78406f75746c6f6f6b2e636f6d222c"
    "22646973706c61795f6e616d65223a22d098d0b2d0b0d0bd20d09fd191d182d18020f09f"
    "93a7222c2269735f616374697665223a747275657d"
)
_VECTOR_EXPECTED_SIG = "b0cfceb2e4ab9d0d49c8893a2a34b397a8e33f422758f7678016f1ce98d24ecc"


def _raw_body() -> bytes:
    # Same serialisation as backend.app.oauth.crm_ingest._serialize.
    return json.dumps(_VECTOR_BODY, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def test_vector_raw_bytes_are_stable() -> None:
    assert _raw_body().hex() == _VECTOR_RAW_HEX


def test_aggregator_signer_matches_fixed_vector() -> None:
    """The aggregator ``build_signature`` over the vector == the pinned hex."""
    assert build_signature(_VECTOR_SECRET, _VECTOR_TS, _raw_body()) == _VECTOR_EXPECTED_SIG


def test_vector_hex_matches_manual_hmac() -> None:
    raw = bytes.fromhex(_VECTOR_RAW_HEX)
    expected = hmac.new(
        _VECTOR_SECRET.encode("utf-8"),
        str(_VECTOR_TS).encode("ascii") + b"." + raw,
        hashlib.sha256,
    ).hexdigest()
    assert expected == _VECTOR_EXPECTED_SIG


def test_non_ascii_reserialization_breaks_signature() -> None:
    raw = _raw_body()
    ascii_variant = json.dumps(_VECTOR_BODY, separators=(",", ":")).encode("utf-8")
    assert raw != ascii_variant
    assert build_signature(_VECTOR_SECRET, _VECTOR_TS, raw) != build_signature(
        _VECTOR_SECRET, _VECTOR_TS, ascii_variant
    )
