"""Unit tests for the reversible login-password crypto (ADR-0038 §2).

``encrypt_user_password`` / ``decrypt_user_password`` reuse the AES-256-GCM
envelope of the mail-password cipher but bind AAD to ``user_id`` under a
distinct domain prefix (``user_pw:``). This suite proves:

- round-trip with the correct ``user_id`` recovers the plaintext;
- a wrong ``user_id`` (AAD mismatch) fails with ``InvalidTag`` — a DB attacker
  cannot move a blob between users;
- domain separation: a mail-password blob or a webhook-secret blob (same key,
  different AAD domain) cannot be decrypted as a user password, and vice
  versa — every cross-domain attempt fails with ``InvalidTag``.

Source of truth: ``shared/crypto.py`` + ADR-0038 §2 + ``docs/06-security.md``
§1.15 / §2.3.
"""

from __future__ import annotations

import os

import pytest
from cryptography.exceptions import InvalidTag

from shared.crypto import (
    USER_PASSWORD_AAD_PREFIX,
    VERSION_CURRENT,
    MailPasswordCipher,
    decrypt_user_password,
    encrypt_user_password,
)

pytestmark = pytest.mark.unit


def _random_key() -> bytes:
    return os.urandom(32)


# ---------------------------------------------------------------------------
# Round-trip (AAD bound to the correct user_id)
# ---------------------------------------------------------------------------


class TestUserPasswordRoundTrip:
    def test_round_trip_correct_user_id(self) -> None:
        cipher = MailPasswordCipher(current_key=_random_key())
        blob = cipher.encrypt_user_password("Hunter2-secret!", user_id=42)
        assert cipher.decrypt_user_password(blob, user_id=42) == "Hunter2-secret!"

    def test_round_trip_unicode(self) -> None:
        cipher = MailPasswordCipher(current_key=_random_key())
        plain = "пароль-оператора-🔒-42"
        blob = cipher.encrypt_user_password(plain, user_id=7)
        assert cipher.decrypt_user_password(blob, user_id=7) == plain

    def test_version_byte_and_fresh_iv(self) -> None:
        cipher = MailPasswordCipher(current_key=_random_key())
        a = cipher.encrypt_user_password("p1-abcdef", user_id=1)
        b = cipher.encrypt_user_password("p1-abcdef", user_id=1)
        assert a[0] == VERSION_CURRENT
        assert a != b  # fresh per-record IV → different ciphertext

    def test_zero_or_negative_user_id_rejected(self) -> None:
        cipher = MailPasswordCipher(current_key=_random_key())
        with pytest.raises(ValueError):
            cipher.encrypt_user_password("p", user_id=0)
        with pytest.raises(ValueError):
            cipher.encrypt_user_password("p", user_id=-5)

    def test_module_wrappers_round_trip_via_settings_key(self) -> None:
        """The public module wrappers build the cipher from Settings and must
        round-trip under the configured ``MAIL_ENCRYPTION_KEY``."""
        blob = encrypt_user_password("Operator-Pass-99", user_id=123)
        assert blob[0] == VERSION_CURRENT
        assert decrypt_user_password(blob, user_id=123) == "Operator-Pass-99"


# ---------------------------------------------------------------------------
# AAD binding — wrong user_id must fail
# ---------------------------------------------------------------------------


class TestUserPasswordAADBinding:
    def test_wrong_user_id_fails_invalid_tag(self) -> None:
        """A blob written for user A cannot be decrypted under user B's AAD."""
        cipher = MailPasswordCipher(current_key=_random_key())
        blob = cipher.encrypt_user_password("secret-of-A-1", user_id=100)
        with pytest.raises(InvalidTag):
            cipher.decrypt_user_password(blob, user_id=101)

    def test_wrong_user_id_via_module_wrappers(self) -> None:
        blob = encrypt_user_password("secret-of-A-2", user_id=200)
        with pytest.raises(InvalidTag):
            decrypt_user_password(blob, user_id=201)


# ---------------------------------------------------------------------------
# Domain separation — a mail-password / webhook-secret blob is NOT a user pw
# ---------------------------------------------------------------------------


class TestUserPasswordDomainSeparation:
    def test_mail_password_blob_not_decryptable_as_user_password(self) -> None:
        """Same key + same id, but the mail-password AAD domain differs from
        the user-password domain → decrypt fails with InvalidTag."""
        cipher = MailPasswordCipher(current_key=_random_key())
        mail_blob = cipher.encrypt("mail-mailbox-pw-1", mail_account_id=5)
        with pytest.raises(InvalidTag):
            cipher.decrypt_user_password(mail_blob, user_id=5)

    def test_webhook_secret_blob_not_decryptable_as_user_password(self) -> None:
        cipher = MailPasswordCipher(current_key=_random_key())
        wh_blob = cipher.encrypt_webhook_secret("wh-secret-value-1", webhook_id=5)
        with pytest.raises(InvalidTag):
            cipher.decrypt_user_password(wh_blob, user_id=5)

    def test_user_password_blob_not_decryptable_as_mail_password(self) -> None:
        cipher = MailPasswordCipher(current_key=_random_key())
        user_blob = cipher.encrypt_user_password("user-login-pw-1", user_id=5)
        with pytest.raises(InvalidTag):
            cipher.decrypt(user_blob, mail_account_id=5)

    def test_user_password_blob_not_decryptable_as_webhook_secret(self) -> None:
        cipher = MailPasswordCipher(current_key=_random_key())
        user_blob = cipher.encrypt_user_password("user-login-pw-2", user_id=5)
        with pytest.raises(InvalidTag):
            cipher.decrypt_webhook_secret(user_blob, webhook_id=5)

    def test_domain_prefix_is_distinct(self) -> None:
        """Guard the ADR-0038 invariant: the user-pw AAD prefix must not
        collide with the mail / webhook domains."""
        assert USER_PASSWORD_AAD_PREFIX == b"user_pw:"
        assert USER_PASSWORD_AAD_PREFIX != b"mail_account_password|"
        assert USER_PASSWORD_AAD_PREFIX != b"webhook_secret|"
