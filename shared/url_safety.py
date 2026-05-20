"""SSRF guard for outbound webhook URLs (ADR-0023 §4.3).

Symmetric in spirit to :mod:`backend.app.security` (IMAP/SMTP host check)
but operates at the URL level: lexical parse + scheme/host/port checks
**before** DNS resolution, and a DNS-resolve sanity check rejecting any
A/AAAA record that lands in a private CIDR.

In ``APP_ENV=dev`` the DNS-resolve check is skipped (consistent with the
IMAP/SMTP behaviour) so a local mock receiver on ``127.0.0.1`` is usable
in development; the lexical-parse rejects of ``localhost`` / ``127.0.0.1``
/ ``0.0.0.0`` / ``[::1]`` literals **are** kept in dev too because they
indicate misconfiguration regardless of environment (the operator should
use an explicit tunnel URL instead of a literal that would silently break
in prod).
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

from shared.config import get_settings

__all__ = [
    "WebhookUrlError",
    "validate_outbound_url",
]


# Same CIDRs as :mod:`backend.app.security` — IMAP/SMTP SSRF guard.
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

# Lexical-parse blocklist: literal host strings that must always be
# rejected, even in dev (ADR-0023 §4.3 "Lexical-parse запрет").
_LITERAL_BLOCKLIST: frozenset[str] = frozenset(
    {
        "localhost",
        "127.0.0.1",
        "0.0.0.0",
        "::1",
        "[::1]",
    }
)

_URL_MAX_LEN: int = 2048
_URL_MIN_LEN: int = 9  # "https://x" minimum


class WebhookUrlError(ValueError):
    """Raised when an outbound webhook URL fails SSRF / lexical checks.

    Carries a stable ``reason`` string for the API layer to translate
    into a domain error envelope (e.g. ``webhook_url_private_ip``).
    """

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


def _is_private(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv4Address):
        return any(ip in net for net in _PRIVATE_NETS_V4)
    return any(ip in net for net in _PRIVATE_NETS_V6)


def _resolve_addresses(host: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Return parsed A/AAAA addresses for ``host``.

    Raises :class:`WebhookUrlError` on DNS failure.
    """
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM, flags=socket.AI_ADDRCONFIG)
    except socket.gaierror as exc:
        raise WebhookUrlError(
            "Could not resolve webhook host",
            reason="dns_failed",
        ) from exc

    seen: set[str] = set()
    out: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for _family, _stype, _proto, _name, sockaddr in infos:
        ip_str = sockaddr[0]
        if ip_str in seen:
            continue
        seen.add(ip_str)
        # IPv6 with scope id (``fe80::1%eth0``) — strip scope before parse.
        candidate = ip_str.split("%", 1)[0] if "%" in ip_str else ip_str
        try:
            out.append(ipaddress.ip_address(candidate))
        except ValueError:
            # Defensive: should never happen for getaddrinfo output but
            # skipping is the safest action here.
            continue
    return out


def validate_outbound_url(url: str) -> str:
    """Validate a webhook URL; return the normalised value on success.

    Performs:

    1. Lexical parse — must be ``https://``, length 9..2048, with a host.
    2. Host blocklist — rejects ``localhost`` / ``127.0.0.1`` / ``0.0.0.0``
       / ``::1`` literally (no DNS needed; happens in dev too).
    3. Port — 443 by default; only TCP ports 1..65535 accepted.
    4. DNS resolve (prod only) — every A/AAAA record must lie outside the
       private CIDR list.

    Returns the URL string as provided (no normalisation; we don't want
    to silently rewrite the user's input).

    Raises :class:`WebhookUrlError` on any failure with a stable
    :attr:`WebhookUrlError.reason` for the caller to map to error codes.
    """
    if not isinstance(url, str):
        raise WebhookUrlError("URL must be a string", reason="invalid_type")
    if not url:
        raise WebhookUrlError("URL must not be empty", reason="empty")
    if len(url) < _URL_MIN_LEN:
        raise WebhookUrlError("URL is too short", reason="too_short")
    if len(url) > _URL_MAX_LEN:
        raise WebhookUrlError("URL is too long", reason="too_long")

    try:
        parsed = urlsplit(url)
    except ValueError as exc:
        raise WebhookUrlError("URL is not a valid URI", reason="invalid_uri") from exc

    if parsed.scheme != "https":
        raise WebhookUrlError("URL scheme must be https://", reason="scheme_not_https")

    host = parsed.hostname
    if not host:
        raise WebhookUrlError("URL must include a host", reason="missing_host")

    # ``parsed.hostname`` lower-cases the netloc; we normalise the literal
    # blocklist accordingly. Square-bracketed IPv6 literals (e.g.
    # ``[::1]``) have already been stripped by urlsplit, so compare to the
    # bare form too.
    host_normalised = host.lower()
    if host_normalised in _LITERAL_BLOCKLIST or f"[{host_normalised}]" in _LITERAL_BLOCKLIST:
        raise WebhookUrlError(
            "URL host is a loopback / non-routable literal",
            reason="webhook_url_private_ip",
        )

    # Port validation — urlsplit raises ``ValueError`` on a non-integer port
    # via the ``port`` property descriptor, so wrap.
    try:
        port = parsed.port
    except ValueError as exc:
        raise WebhookUrlError("URL port is not an integer", reason="invalid_port") from exc
    if port is not None and not (1 <= port <= 65535):
        raise WebhookUrlError("URL port must be 1..65535", reason="invalid_port")

    # If the host is already an IP literal, validate it directly without
    # paying for a DNS round-trip.
    try:
        ip_literal = ipaddress.ip_address(host_normalised.strip("[]"))
    except ValueError:
        ip_literal = None  # not an IP literal — fall through to DNS resolve

    settings = get_settings()
    if ip_literal is not None:
        if _is_private(ip_literal):
            raise WebhookUrlError(
                "URL host resolves to a private/internal address",
                reason="webhook_url_private_ip",
            )
        return url

    # Hostname — DNS resolve in prod. Dev skips this for parity with IMAP/SMTP
    # behaviour (ADR-0023 §4.3 "Dev override").
    if not settings.is_prod:
        return url

    addresses = _resolve_addresses(host_normalised)
    if not addresses:
        raise WebhookUrlError(
            "Webhook host has no A/AAAA records",
            reason="dns_failed",
        )
    for ip in addresses:
        if _is_private(ip):
            raise WebhookUrlError(
                "URL host resolves to a private/internal address",
                reason="webhook_url_private_ip",
            )
    return url
