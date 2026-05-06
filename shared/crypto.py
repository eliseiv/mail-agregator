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
    "encrypt_mail_password",
]

VERSION_CURRENT = 0x01
VERSION_PREV = 0x00
IV_LEN = 12
HEADER_LEN = 1 + IV_LEN  # version + iv
AAD_PREFIX = b"mail_account_password|"


def _aad_for(mail_account_id: int) -> bytes:
    if not mail_account_id or mail_account_id <= 0:
        raise ValueError("mail_account_id must be a positive integer for AAD")
    return AAD_PREFIX + str(mail_account_id).encode("ascii")


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
        if plaintext is None:
            raise ValueError("plaintext must not be None")
        if not isinstance(plaintext, str):
            raise TypeError("plaintext must be str")
        aad = _aad_for(mail_account_id)
        iv = os.urandom(IV_LEN)
        ct = self._current.encrypt(iv, plaintext.encode("utf-8"), aad)
        return bytes([VERSION_CURRENT]) + iv + ct

    def decrypt(self, blob: bytes, mail_account_id: int) -> str:
        if not blob or len(blob) < HEADER_LEN + 16:  # 16 = GCM tag
            raise InvalidTag("blob too short")
        version = blob[0]
        iv = blob[1:HEADER_LEN]
        ct = blob[HEADER_LEN:]
        aad = _aad_for(mail_account_id)

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
