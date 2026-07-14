"""Local mock IMAP/SMTP servers for the ADR-0047 hard-deadline tests.

These are *external boundaries* (a third-party mail server), so mocking them is
allowed; nothing of our own code is mocked. Each server binds ``127.0.0.1:0``
(a free port picked by the OS) so parallel runs never collide.

Three shapes are needed by ADR-0047 / ``05-modules.md`` §21:

- :func:`silent_server` — accepts the TCP connection and never writes a byte.
  Used as the hung IMAP server (no ``* OK`` greeting → ``imaplib`` blocks) and
  as the hung SMTP server (no ``220`` banner → ``aiosmtplib.connect`` blocks).
- :func:`smtp_banner_then_silent` — sends the ``220`` banner (so the client is
  CONNECTED) and then never answers anything again, including ``QUIT``. This is
  the only shape that exercises the teardown leg of ADR-0047 §2.3: the deadline
  cancels the probe, ``finally: await _close_smtp_client(client)`` sends a
  polite ``QUIT`` to a server that will never reply, and ``_SMTP_QUIT_TIMEOUT``
  (5 s) — not ``_SMTP_TIMEOUT`` (20 s) — has to bound it.
- :func:`imap_server_ok` — a minimal, working IMAP server (greeting →
  ``CAPABILITY`` → ``LOGIN`` → ``SELECT INBOX`` → ``LOGOUT``). Needed so a probe
  can reach the SMTP stage with IMAP already green (stage attribution).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import asynccontextmanager
from typing import Any

Handler = Callable[[asyncio.StreamReader, asyncio.StreamWriter], Coroutine[Any, Any, None]]

#: ``(host, port)`` of a running mock server.
Endpoint = tuple[str, int]


@asynccontextmanager
async def _serve(handler: Handler) -> AsyncIterator[Endpoint]:
    """Run ``handler`` as a TCP server on an ephemeral loopback port."""

    async def _guarded(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await handler(reader, writer)
        except (ConnectionError, asyncio.CancelledError, OSError):
            pass
        finally:
            with contextlib.suppress(Exception):
                writer.close()

    server = await asyncio.start_server(_guarded, "127.0.0.1", 0)
    host, port = server.sockets[0].getsockname()[:2]
    try:
        yield (host, port)
    finally:
        server.close()
        with contextlib.suppress(Exception):
            await server.wait_closed()


@asynccontextmanager
async def silent_server() -> AsyncIterator[Endpoint]:
    """Accept the connection, never send a byte. The canonical hung mail host."""

    async def _handle(reader: asyncio.StreamReader, _writer: asyncio.StreamWriter) -> None:
        # Drain whatever the client sends and answer NOTHING, ever.
        while await reader.read(4096):
            pass

    async with _serve(_handle) as endpoint:
        yield endpoint


@asynccontextmanager
async def smtp_banner_then_silent(received: list[bytes]) -> AsyncIterator[Endpoint]:
    """Send the ``220`` banner, then go silent forever — including on ``QUIT``.

    ``received`` collects every line the client sends, so a test can prove the
    polite ``QUIT`` of the teardown leg (ADR-0047 §2.3) actually reached the
    server and was then bounded by ``_SMTP_QUIT_TIMEOUT`` rather than by
    ``_SMTP_TIMEOUT``.
    """

    async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(b"220 mock.smtp.invalid ESMTP ready\r\n")
        await writer.drain()
        # The client is now CONNECTED. Record its commands (EHLO, ..., QUIT) but
        # never reply again.
        while True:
            line = await reader.readline()
            if not line:
                return
            received.append(line.strip())

    async with _serve(_handle) as endpoint:
        yield endpoint


@asynccontextmanager
async def imap_server_ok() -> AsyncIterator[Endpoint]:
    """Minimal working IMAP4rev1 server: greeting, CAPABILITY, LOGIN, SELECT, LOGOUT."""

    async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(b"* OK [CAPABILITY IMAP4rev1] mock IMAP ready\r\n")
        await writer.drain()
        while True:
            line = await reader.readline()
            if not line:
                return
            parts = line.decode("utf-8", "replace").split()
            if not parts:
                continue
            tag = parts[0]
            cmd = parts[1].upper() if len(parts) > 1 else ""
            if cmd == "CAPABILITY":
                writer.write(b"* CAPABILITY IMAP4rev1\r\n")
                writer.write(f"{tag} OK CAPABILITY completed\r\n".encode())
            elif cmd == "LOGIN":
                writer.write(f"{tag} OK LOGIN completed\r\n".encode())
            elif cmd == "SELECT":
                writer.write(b"* 0 EXISTS\r\n* 0 RECENT\r\n")
                writer.write(b"* OK [UIDVALIDITY 1] UIDs valid\r\n")
                writer.write(b"* OK [UIDNEXT 1] Predicted next UID\r\n")
                writer.write(f"{tag} OK [READ-WRITE] SELECT completed\r\n".encode())
            elif cmd == "LOGOUT":
                writer.write(b"* BYE logging out\r\n")
                writer.write(f"{tag} OK LOGOUT completed\r\n".encode())
                await writer.drain()
                return
            else:
                writer.write(f"{tag} OK {cmd} completed\r\n".encode())
            await writer.drain()

    async with _serve(_handle) as endpoint:
        yield endpoint
