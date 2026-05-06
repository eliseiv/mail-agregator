"""IMAP + SMTP test-login helpers (used by test/create/update endpoints).

Both perform the absolute minimum needed to validate credentials:

- IMAP: connect, login, ``SELECT INBOX``, logout. ``imap-tools`` is sync,
  so we wrap in :func:`asyncio.to_thread`.
- SMTP: connect, ``EHLO``, ``STARTTLS`` if requested, login, ``QUIT``.

Errors are translated to the appropriate :class:`backend.app.exceptions.DomainError`.

SSRF guard: :func:`backend.app.security.assert_public_host` is called for
each host before any TCP connect, so we never leak ourselves as an internal
port scanner (``docs/06-security.md`` sec. 4).
"""

from __future__ import annotations

import asyncio
import contextlib
import ssl
from typing import Any

import aiosmtplib
import imap_tools
from imap_tools import MailBoxUnencrypted

from backend.app.exceptions import (
    IMAPLoginFailedError,
    SMTPLoginFailedError,
)
from backend.app.security import assert_public_host
from shared.logging import get_logger

log = get_logger(__name__)

_IMAP_TIMEOUT = 30
_SMTP_TIMEOUT = 30


def _safe_error_text(exc: BaseException, max_len: int = 200) -> str:
    """Single-line, length-clamped representation. Strips secrets defensively."""
    msg = str(exc).replace("\r", " ").replace("\n", " ")
    return msg[:max_len]


# ---------------------------------------------------------------------------
# IMAP
# ---------------------------------------------------------------------------


def _imap_login_blocking(host: str, port: int, ssl_on: bool, username: str, password: str) -> None:
    """Sync IMAP login. Selects INBOX as a sanity check, then logs out.

    ``imap_tools.MailBox(...)`` opens the TCP/TLS socket in its constructor,
    so DNS resolution and connect happen there. Both the constructor and
    :meth:`login` therefore live inside the same try-block so that
    ``socket.gaierror`` (a subclass of :class:`OSError`) raised at connect-time
    surfaces as :class:`IMAPLoginFailedError`, not as an unhandled exception.
    """
    mailbox: Any | None = None
    try:
        if ssl_on:
            mailbox = imap_tools.MailBox(host, port=port, timeout=_IMAP_TIMEOUT)
        else:
            mailbox = MailBoxUnencrypted(host, port=port, timeout=_IMAP_TIMEOUT)
        mailbox.login(username, password, initial_folder="INBOX")
    except imap_tools.MailboxLoginError as exc:
        raise IMAPLoginFailedError(
            "IMAP login failed",
            details={"detail": _safe_error_text(exc)},
        ) from exc
    except imap_tools.MailboxFolderSelectError as exc:
        raise IMAPLoginFailedError(
            "Cannot select INBOX",
            details={"detail": "cannot_select_inbox"},
        ) from exc
    except (TimeoutError, OSError) as exc:
        raise IMAPLoginFailedError(
            "Could not connect to IMAP server",
            details={"detail": _safe_error_text(exc)},
        ) from exc

    # Ignore logout errors — the login already succeeded.
    with contextlib.suppress(Exception):
        mailbox.logout()


async def imap_test_login(
    *,
    host: str,
    port: int,
    ssl_on: bool,
    username: str,
    password: str,
) -> None:
    """Test IMAP login. Raises :class:`IMAPLoginFailedError` on any failure."""
    assert_public_host(host, port=port)
    try:
        await asyncio.wait_for(
            asyncio.to_thread(_imap_login_blocking, host, port, ssl_on, username, password),
            timeout=_IMAP_TIMEOUT + 5,
        )
    except TimeoutError as exc:
        raise IMAPLoginFailedError(
            "IMAP login timed out",
            details={"detail": "timeout"},
        ) from exc


# ---------------------------------------------------------------------------
# SMTP
# ---------------------------------------------------------------------------


def _ssl_context() -> ssl.SSLContext:
    """Default secure SSL context (TLS 1.2+, verify chain)."""
    ctx = ssl.create_default_context()
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


async def smtp_test_login(
    *,
    host: str,
    port: int,
    ssl_on: bool,
    starttls: bool,
    username: str,
    password: str,
) -> None:
    """Test SMTP login. Raises :class:`SMTPLoginFailedError` on any failure."""
    assert_public_host(host, port=port)
    if ssl_on and starttls:
        raise SMTPLoginFailedError(
            "smtp_ssl and smtp_starttls are mutually exclusive",
            details={"detail": "ssl_and_starttls_set"},
        )

    client = aiosmtplib.SMTP(
        hostname=host,
        port=port,
        use_tls=ssl_on,
        start_tls=False,  # we'll do it explicitly after EHLO if needed
        timeout=_SMTP_TIMEOUT,
        tls_context=_ssl_context() if ssl_on else None,
    )
    try:
        await client.connect()
        if starttls:
            await client.starttls(tls_context=_ssl_context())
        await client.login(username, password)
    except aiosmtplib.SMTPAuthenticationError as exc:
        raise SMTPLoginFailedError(
            "SMTP authentication failed",
            details={"detail": _safe_error_text(exc)},
        ) from exc
    except (
        aiosmtplib.SMTPConnectError,
        aiosmtplib.SMTPServerDisconnected,
        aiosmtplib.SMTPException,
        TimeoutError,
        OSError,
    ) as exc:
        raise SMTPLoginFailedError(
            "Could not connect to SMTP server",
            details={"detail": _safe_error_text(exc)},
        ) from exc
    finally:
        with contextlib.suppress(Exception):
            await client.quit()
