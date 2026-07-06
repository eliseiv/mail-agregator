"""AES-256-GCM envelope for mail account passwords (ADR-0005, ``docs/05-modules.md`` sec. 5).

Blob format::

    version_byte (1 B) || iv (12 B) || ciphertext_with_tag (variable)

``version_byte`` selects the master key:
- ``0x01`` -> ``MAIL_ENCRYPTION_KEY`` (current)
- ``0x00`` -> ``MAIL_ENCRYPTION_KEY_PREV`` (only valid during rotation)

AAD is bound to the ``mail_account_id`` so an attacker with DB access can't
move blobs between rows (decrypt fails with ``InvalidTag``).

INSERT path: callers SELECT ``nextval('mail_accounts_id_seq')`` first to
get the future id, encrypt with that id in the AAD, then INSERT with the
explicit id. See ``backend.app.repositories.mail_accounts.next_account_id``.
"""

from __future__ import annotations

import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from shared.config import Settings, get_settings

# Public re-export so callers can `except InvalidTag:` without importing
# directly from `cryptography`.
__all__ = [
    "InvalidTag",
    "MailPasswordCipher",
    "decrypt_mail_password",
    "decrypt_user_password",
    "decrypt_webhook_secret",
    "encrypt_mail_password",
    "encrypt_user_password",
    "encrypt_webhook_secret",
]

VERSION_CURRENT = 0x01
VERSION_PREV = 0x00
IV_LEN = 12
HEADER_LEN = 1 + IV_LEN  # version + iv
AAD_PREFIX = b"mail_account_password|"
# ADR-0023 §4.1: webhook secrets reuse the same AES-256-GCM primitive but
# bind AAD to ``webhook_id`` so an attacker with DB access cannot swap
# ciphertexts between two webhook rows (decrypt fails with ``InvalidTag``).
WEBHOOK_SECRET_AAD_PREFIX = b"webhook_secret|"
# ADR-0038 §2: reversible copy of a user's login password reuses the same
# AES-256-GCM primitive/key but binds AAD to ``user_id`` under a distinct
# domain prefix. Domain separation from ``mail_account_password|`` /
# ``webhook_secret|`` means a DB attacker cannot substitute a mail-password
# or webhook-secret blob into ``users.password_encrypted`` (decrypt fails
# with ``InvalidTag``), and the id-binding blocks swapping blobs between
# users.
USER_PASSWORD_AAD_PREFIX = b"user_pw:"


def _aad_for(mail_account_id: int) -> bytes:
    if not mail_account_id or mail_account_id <= 0:
        raise ValueError("mail_account_id must be a positive integer for AAD")
    return AAD_PREFIX + str(mail_account_id).encode("ascii")


def _webhook_aad(webhook_id: int) -> bytes:
    if not webhook_id or webhook_id <= 0:
        raise ValueError("webhook_id must be a positive integer for AAD")
    return WEBHOOK_SECRET_AAD_PREFIX + str(webhook_id).encode("ascii")


def _user_password_aad(user_id: int) -> bytes:
    if not user_id or user_id <= 0:
        raise ValueError("user_id must be a positive integer for AAD")
    return USER_PASSWORD_AAD_PREFIX + str(user_id).encode("ascii")


class MailPasswordCipher:
    """Encrypt/decrypt mail-account passwords using the configured master keys.

    Designed as an instance so tests can construct it with explicit keys.
    Production code uses :func:`encrypt_mail_password` / :func:`decrypt_mail_password`
    which build it from :class:`shared.config.Settings`.
    """

    def __init__(self, current_key: bytes, prev_key: bytes | None = None) -> None:
        if len(current_key) != 32:
            raise ValueError("current_key must be 32 bytes")
        if prev_key is not None and len(prev_key) != 32:
            raise ValueError("prev_key must be 32 bytes (or None)")
        self._current = AESGCM(current_key)
        self._prev = AESGCM(prev_key) if prev_key is not None else None

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> MailPasswordCipher:
        s = settings or get_settings()
        return cls(
            current_key=s.mail_master_key_bytes(),
            prev_key=s.mail_master_key_prev_bytes(),
        )

    def encrypt(self, plaintext: str, mail_account_id: int) -> bytes:
        return self._encrypt_with_aad(plaintext, _aad_for(mail_account_id))

    def decrypt(self, blob: bytes, mail_account_id: int) -> str:
        return self._decrypt_with_aad(blob, _aad_for(mail_account_id))

    def encrypt_webhook_secret(self, plaintext: str, webhook_id: int) -> bytes:
        """Encrypt a webhook secret (ADR-0023 §4.1).

        Uses the same envelope as :meth:`encrypt` but binds AAD to
        ``webhook_id`` via :data:`WEBHOOK_SECRET_AAD_PREFIX`.
        """
        return self._encrypt_with_aad(plaintext, _webhook_aad(webhook_id))

    def decrypt_webhook_secret(self, blob: bytes, webhook_id: int) -> str:
        """Decrypt a webhook secret (ADR-0023 §4.1)."""
        return self._decrypt_with_aad(blob, _webhook_aad(webhook_id))

    def encrypt_user_password(self, plaintext: str, user_id: int) -> bytes:
        """Encrypt a user's login password (ADR-0038 §2).

        Uses the same envelope as :meth:`encrypt` but binds AAD to
        ``user_id`` via :data:`USER_PASSWORD_AAD_PREFIX`.
        """
        return self._encrypt_with_aad(plaintext, _user_password_aad(user_id))

    def decrypt_user_password(self, blob: bytes, user_id: int) -> str:
        """Decrypt a user's login password (ADR-0038 §2)."""
        return self._decrypt_with_aad(blob, _user_password_aad(user_id))

    # --- Internal AAD-parameterised primitives -----------------------------

    def _encrypt_with_aad(self, plaintext: str, aad: bytes) -> bytes:
        if plaintext is None:
            raise ValueError("plaintext must not be None")
        if not isinstance(plaintext, str):
            raise TypeError("plaintext must be str")
        iv = os.urandom(IV_LEN)
        ct = self._current.encrypt(iv, plaintext.encode("utf-8"), aad)
        return bytes([VERSION_CURRENT]) + iv + ct

    def _decrypt_with_aad(self, blob: bytes, aad: bytes) -> str:
        if not blob or len(blob) < HEADER_LEN + 16:  # 16 = GCM tag
            raise InvalidTag("blob too short")
        version = blob[0]
        iv = blob[1:HEADER_LEN]
        ct = blob[HEADER_LEN:]

        if version == VERSION_CURRENT:
            cipher = self._current
        elif version == VERSION_PREV:
            if self._prev is None:
                raise InvalidTag("blob version=0x00 but MAIL_ENCRYPTION_KEY_PREV not configured")
            cipher = self._prev
        else:
            raise InvalidTag(f"unknown version byte: 0x{version:02x}")

        plain = cipher.decrypt(iv, ct, aad)
        return plain.decode("utf-8")


# --- Module-level convenience wrappers --------------------------------------


def encrypt_mail_password(plaintext: str, mail_account_id: int) -> bytes:
    """Encrypt a plain mail-account password.

    See :class:`MailPasswordCipher` for format details.
    """
    return MailPasswordCipher.from_settings().encrypt(plaintext, mail_account_id)


def decrypt_mail_password(blob: bytes, mail_account_id: int) -> str:
    """Decrypt a mail-account password blob.

    Raises :class:`cryptography.exceptions.InvalidTag` if the AAD doesn't match
    the row id, the blob is corrupted, or the wrong key is configured.
    """
    return MailPasswordCipher.from_settings().decrypt(blob, mail_account_id)


def encrypt_webhook_secret(plaintext: str, webhook_id: int) -> bytes:
    """Encrypt a webhook secret (ADR-0023 §4.1).

    AAD binds the ciphertext to ``webhook_id`` so a DB attacker cannot
    move a row's blob to another webhook.
    """
    return MailPasswordCipher.from_settings().encrypt_webhook_secret(plaintext, webhook_id)


def decrypt_webhook_secret(blob: bytes, webhook_id: int) -> str:
    """Decrypt a webhook secret blob (ADR-0023 §4.1).

    Raises :class:`cryptography.exceptions.InvalidTag` on mismatch.
    """
    return MailPasswordCipher.from_settings().decrypt_webhook_secret(blob, webhook_id)


def encrypt_user_password(plaintext: str, user_id: int) -> bytes:
    """Encrypt a user's login password for reversible admin display (ADR-0038 §2).

    AAD binds the ciphertext to ``user_id`` so a DB attacker cannot move a
    row's blob to another user (or substitute a mail-password / webhook
    secret blob — different AAD domain).
    """
    return MailPasswordCipher.from_settings().encrypt_user_password(plaintext, user_id)


def decrypt_user_password(blob: bytes, user_id: int) -> str:
    """Decrypt a user's login password blob (ADR-0038 §2).

    Raises :class:`cryptography.exceptions.InvalidTag` if the AAD doesn't
    match the ``user_id``, the blob is corrupted, or the wrong key is
    configured.
    """
    return MailPasswordCipher.from_settings().decrypt_user_password(blob, user_id)
