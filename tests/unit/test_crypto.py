"""Unit tests for ``shared.crypto`` — AES-256-GCM envelope, AAD binding,
key versioning, key rotation, blob format.

Source of truth: ``shared/crypto.py`` + ADR-0005 + ``docs/06-security.md`` sec.6.
"""

from __future__ import annotations

import os

import pytest
from cryptography.exceptions import InvalidTag

from shared.crypto import (
    HEADER_LEN,
    IV_LEN,
    VERSION_CURRENT,
    VERSION_PREV,
    MailPasswordCipher,
)

pytestmark = pytest.mark.unit


def _random_key() -> bytes:
    return os.urandom(32)


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------


class TestRoundTrip:
    def test_encrypt_decrypt_round_trip(self) -> None:
        cipher = MailPasswordCipher(current_key=_random_key())
        blob = cipher.encrypt("super-secret-password", mail_account_id=42)
        assert cipher.decrypt(blob, mail_account_id=42) == "super-secret-password"

    def test_round_trip_unicode_password(self) -> None:
        cipher = MailPasswordCipher(current_key=_random_key())
        plain = "пароль-с-кириллицей-и-emoji-🔒"
        blob = cipher.encrypt(plain, mail_account_id=1)
        assert cipher.decrypt(blob, mail_account_id=1) == plain

    def test_round_trip_long_password(self) -> None:
        cipher = MailPasswordCipher(current_key=_random_key())
        plain = "x" * 200
        blob = cipher.encrypt(plain, mail_account_id=1)
        assert cipher.decrypt(blob, mail_account_id=1) == plain

    def test_round_trip_empty_password(self) -> None:
        cipher = MailPasswordCipher(current_key=_random_key())
        blob = cipher.encrypt("", mail_account_id=1)
        assert cipher.decrypt(blob, mail_account_id=1) == ""

    def test_each_encrypt_uses_fresh_iv(self) -> None:
        """Same plaintext + key + AAD must produce different ciphertexts."""
        cipher = MailPasswordCipher(current_key=_random_key())
        a = cipher.encrypt("p", mail_account_id=1)
        b = cipher.encrypt("p", mail_account_id=1)
        assert a != b
        # Specifically, the IV portion differs.
        assert a[1:HEADER_LEN] != b[1:HEADER_LEN]


# ---------------------------------------------------------------------------
# AAD binding
# ---------------------------------------------------------------------------


class TestAADBinding:
    def test_decrypt_with_different_aad_fails(self) -> None:
        """An attacker who moves a blob to a different mail_account row sees InvalidTag."""
        cipher = MailPasswordCipher(current_key=_random_key())
        blob = cipher.encrypt("p", mail_account_id=42)
        with pytest.raises(InvalidTag):
            cipher.decrypt(blob, mail_account_id=43)

    def test_decrypt_zero_id_rejected(self) -> None:
        cipher = MailPasswordCipher(current_key=_random_key())
        with pytest.raises(ValueError):
            cipher.encrypt("p", mail_account_id=0)
        with pytest.raises(ValueError):
            cipher.encrypt("p", mail_account_id=-1)


# ---------------------------------------------------------------------------
# Format: version byte + IV layout
# ---------------------------------------------------------------------------


class TestFormat:
    def test_version_byte_is_0x01_for_current_key(self) -> None:
        cipher = MailPasswordCipher(current_key=_random_key())
        blob = cipher.encrypt("p", mail_account_id=1)
        assert blob[0] == VERSION_CURRENT

    def test_iv_length_is_12_bytes(self) -> None:
        cipher = MailPasswordCipher(current_key=_random_key())
        blob = cipher.encrypt("p", mail_account_id=1)
        # Header = version (1) + IV (12) = 13 bytes.
        assert len(blob) >= HEADER_LEN + 16  # 16 = GCM tag
        assert IV_LEN == 12

    def test_short_blob_raises_invalid_tag(self) -> None:
        cipher = MailPasswordCipher(current_key=_random_key())
        with pytest.raises(InvalidTag):
            cipher.decrypt(b"\x01" + b"x" * 5, mail_account_id=1)
        with pytest.raises(InvalidTag):
            cipher.decrypt(b"", mail_account_id=1)

    def test_unknown_version_byte_rejected(self) -> None:
        cipher = MailPasswordCipher(current_key=_random_key())
        # Build a blob with a bogus version 0x77.
        bogus = bytes([0x77]) + b"\x00" * IV_LEN + b"\x00" * 32
        with pytest.raises(InvalidTag):
            cipher.decrypt(bogus, mail_account_id=1)


# ---------------------------------------------------------------------------
# Key rotation: previous key honoured for blobs tagged 0x00
# ---------------------------------------------------------------------------


class TestKeyRotation:
    def test_decrypt_with_prev_key_when_version_byte_is_zero(self) -> None:
        prev = _random_key()
        curr = _random_key()
        # Encrypt with the "previous" cipher, then rewrite the version byte.
        # We model the rotation: a blob written when prev was current.
        old_cipher = MailPasswordCipher(current_key=prev)
        old_blob = old_cipher.encrypt("legacy", mail_account_id=7)
        # Rewrite version byte to 0x00 to signal "use prev".
        legacy_blob = bytes([VERSION_PREV]) + old_blob[1:]
        new_cipher = MailPasswordCipher(current_key=curr, prev_key=prev)
        assert new_cipher.decrypt(legacy_blob, mail_account_id=7) == "legacy"

    def test_decrypt_legacy_blob_without_prev_configured_fails(self) -> None:
        cipher = MailPasswordCipher(current_key=_random_key())
        legacy = bytes([VERSION_PREV]) + b"\x00" * IV_LEN + b"\x00" * 32
        with pytest.raises(InvalidTag):
            cipher.decrypt(legacy, mail_account_id=1)


# ---------------------------------------------------------------------------
# Constructor validation
# ---------------------------------------------------------------------------


class TestConstructor:
    def test_current_key_must_be_32_bytes(self) -> None:
        with pytest.raises(ValueError):
            MailPasswordCipher(current_key=b"too-short")
        with pytest.raises(ValueError):
            MailPasswordCipher(current_key=b"x" * 33)

    def test_prev_key_must_be_32_bytes_when_set(self) -> None:
        with pytest.raises(ValueError):
            MailPasswordCipher(current_key=_random_key(), prev_key=b"too-short")

    def test_plaintext_type_check(self) -> None:
        cipher = MailPasswordCipher(current_key=_random_key())
        with pytest.raises(TypeError):
            cipher.encrypt(b"bytes-not-str", mail_account_id=1)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            cipher.encrypt(None, mail_account_id=1)  # type: ignore[arg-type]
