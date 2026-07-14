"""Cross-cutting security helpers: SSRF guard for IMAP/SMTP hosts.

Implements ``docs/06-security.md`` sec. 4.

In ``APP_ENV=dev`` private IPs are allowed (lets us point at a local mock
mail server). In ``prod`` any private/loopback/link-local target is rejected
with :class:`backend.app.exceptions.InvalidHostError`.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Iterable

from backend.app.exceptions import InvalidHostError
from shared.config import get_settings
from shared.logging import get_logger

log = get_logger(__name__)

_PRIVATE_NETS_V4: tuple[ipaddress.IPv4Network, ...] = (
    ipaddress.IPv4Network("10.0.0.0/8"),
    ipaddress.IPv4Network("172.16.0.0/12"),
    ipaddress.IPv4Network("192.168.0.0/16"),
    ipaddress.IPv4Network("127.0.0.0/8"),
    ipaddress.IPv4Network("169.254.0.0/16"),
    ipaddress.IPv4Network("0.0.0.0/8"),
    ipaddress.IPv4Network("100.64.0.0/10"),
)
_PRIVATE_NETS_V6: tuple[ipaddress.IPv6Network, ...] = (
    ipaddress.IPv6Network("::1/128"),
    ipaddress.IPv6Network("fc00::/7"),
    ipaddress.IPv6Network("fe80::/10"),
)


def _is_private(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv4Address):
        return any(ip in net for net in _PRIVATE_NETS_V4)
    return any(ip in net for net in _PRIVATE_NETS_V6)


def _resolve(host: str) -> Iterable[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Resolve A + AAAA records and yield parsed addresses.

    ``getaddrinfo`` is sync; in our flows we only call this from already-async
    contexts but the call is bounded by the OS resolver and should complete
    in single-digit ms for cached entries. If callers ever loop over many
    hosts, wrap in ``asyncio.to_thread``.
    """
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM, flags=socket.AI_ADDRCONFIG)
    except socket.gaierror as exc:
        raise InvalidHostError(
            "Could not resolve host", details={"host": host, "reason": str(exc)}
        ) from exc

    seen: set[str] = set()
    for family, _stype, _proto, _name, sockaddr in infos:
        ip_str = sockaddr[0]
        if ip_str in seen:
            continue
        seen.add(ip_str)
        try:
            yield ipaddress.ip_address(ip_str)
        except ValueError:
            # ipv6 with scope id — strip scope
            if "%" in ip_str:
                yield ipaddress.ip_address(ip_str.split("%", 1)[0])
            else:
                continue
        del family


def assert_public_host(host: str, *, port: int) -> None:
    """Raise :class:`InvalidHostError` if ``host`` resolves to a private IP.

    No-op in ``APP_ENV=dev`` (so devs can point at ``localhost:1143``).
    """
    if not host:
        raise InvalidHostError("host must be a non-empty string")
    if not (1 <= port <= 65535):
        raise InvalidHostError("port must be in 1..65535", details={"host": host, "port": port})

    settings = get_settings()
    if not settings.is_prod:
        return

    for ip in _resolve(host):
        if _is_private(ip):
            log.warning(
                "ssrf_block",
                host=host,
                resolved_ip=str(ip),
            )
            raise InvalidHostError(
                "Host resolves to a private/internal address",
                details={"host": host},
            )


async def assert_public_host_async(host: str, *, port: int) -> None:
    """Off-loop variant of :func:`assert_public_host` (ADR-0047 §4).

    :func:`assert_public_host` calls the BLOCKING ``socket.getaddrinfo``
    (:func:`_resolve`). Called straight from a coroutine it runs *in the event
    loop thread*: a hung resolver stalls the whole loop, and since
    :func:`asyncio.wait_for` can only cancel at ``await`` points, any deadline
    around such a call is DECORATIVE (ADR-0047 «Дефект 4»). Every async caller
    (connection-test — ADR-0047 §4; send + worker sync — TD-056) therefore
    resolves in a worker thread.

    Semantics are unchanged: :class:`InvalidHostError` raised inside the thread
    propagates to the caller as before. When an outer deadline expires the
    thread may outlive the cancelled ``await`` and finish on its own — the same
    accepted trade-off as the existing ``wait_for``-over-``to_thread`` IMAP
    pattern (``accounts/testers.py``).
    """
    await asyncio.to_thread(assert_public_host, host, port=port)
