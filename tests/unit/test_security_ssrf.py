"""Unit tests for ``backend.app.security`` — SSRF guard.

The guard refuses to connect to private/loopback/link-local addresses in
``APP_ENV=prod``; in ``dev`` it is a no-op so a developer can point at
``localhost:1143``.

We exercise:
- The dev bypass (no-op regardless of host).
- Each private network class (loopback v4/v6, RFC1918 10/172/192,
  link-local 169.254/fe80::, CGNAT 100.64, IPv4 0.0.0.0/8).
- A genuine public address (allowed in prod).
- Resolver errors mapped to InvalidHostError.
- Port range and host validation.

Source of truth: ``backend/app/security.py`` + ``docs/06-security.md`` sec. 4.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Iterable

import pytest

from backend.app import security as sec_mod
from backend.app.exceptions import InvalidHostError
from backend.app.security import assert_public_host

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers — deterministic resolver via monkeypatch
# ---------------------------------------------------------------------------


def _make_fake_resolver(
    host_to_ips: dict[str, list[str]],
) -> object:
    """Return a fake ``socket.getaddrinfo`` that maps host -> list of IPs.

    Returned shape mimics getaddrinfo's tuples: (family, type, proto, name,
    sockaddr) where sockaddr[0] is the IP string. Family is set to a
    plausible value but the production code only reads sockaddr[0].
    """

    def _fake(host: str, *_args: object, **_kwargs: object) -> list[tuple]:
        ips = host_to_ips.get(host)
        if ips is None:
            raise socket.gaierror(socket.EAI_NONAME, "no such host")
        out: list[tuple] = []
        for ip in ips:
            try:
                ipaddress.IPv4Address(ip)
                family = socket.AF_INET
                sockaddr: tuple = (ip, 0)
            except ipaddress.AddressValueError:
                family = socket.AF_INET6
                sockaddr = (ip, 0, 0, 0)
            out.append((family, socket.SOCK_STREAM, 0, "", sockaddr))
        return out

    return _fake


class _ProdSettings:
    """Stand-in for shared.config.Settings — we only read ``is_prod``."""

    is_prod = True


class _DevSettings:
    is_prod = False


@pytest.fixture
def prod_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sec_mod, "get_settings", lambda: _ProdSettings())


@pytest.fixture
def dev_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sec_mod, "get_settings", lambda: _DevSettings())


@pytest.fixture
def resolver(monkeypatch: pytest.MonkeyPatch) -> Iterable[None]:
    """Patch ``socket.getaddrinfo`` to a deterministic mapping per test."""

    def _install(mapping: dict[str, list[str]]) -> None:
        monkeypatch.setattr(socket, "getaddrinfo", _make_fake_resolver(mapping))

    yield _install  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Dev bypass
# ---------------------------------------------------------------------------


class TestDevBypass:
    def test_dev_allows_loopback(self, dev_env: None) -> None:
        # No exception even though 127.0.0.1 is private.
        assert_public_host("localhost", port=1143)

    def test_dev_allows_unresolvable_host(self, dev_env: None) -> None:
        # Dev short-circuits before resolving; gaierror should not surface.
        assert_public_host("imap.example.invalid", port=993)


# ---------------------------------------------------------------------------
# Prod: private addresses are blocked
# ---------------------------------------------------------------------------


class TestProdRejectsPrivate:
    @pytest.mark.parametrize(
        "ip",
        [
            "127.0.0.1",  # loopback
            "127.255.255.254",
            "10.0.0.1",  # RFC1918
            "10.255.255.255",
            "172.16.0.1",  # RFC1918
            "172.31.255.255",
            "192.168.0.1",  # RFC1918
            "192.168.1.42",
            "169.254.0.1",  # link-local
            "169.254.169.254",  # AWS metadata
            "0.0.0.0",  # 0.0.0.0/8
            "0.1.2.3",
            "100.64.0.1",  # CGNAT
            "100.127.255.255",
        ],
    )
    def test_each_ipv4_private_class_blocked(self, prod_env: None, resolver, ip: str) -> None:
        resolver({"target.example.com": [ip]})
        with pytest.raises(InvalidHostError) as ei:
            assert_public_host("target.example.com", port=993)
        assert ei.value.code == "invalid_host"

    @pytest.mark.parametrize(
        "ip",
        [
            "::1",  # ipv6 loopback
            "fc00::1",  # ULA
            "fdff:ffff::1",  # ULA upper
            "fe80::1",  # link-local
            "fe80::dead:beef",
        ],
    )
    def test_each_ipv6_private_class_blocked(self, prod_env: None, resolver, ip: str) -> None:
        resolver({"v6.example.com": [ip]})
        with pytest.raises(InvalidHostError):
            assert_public_host("v6.example.com", port=993)


class TestProdAllowsPublic:
    @pytest.mark.parametrize(
        "ip",
        [
            "8.8.8.8",  # Google
            "1.1.1.1",  # Cloudflare
            "142.250.31.27",  # imap.gmail.com (one of)
        ],
    )
    def test_public_ipv4_allowed(self, prod_env: None, resolver, ip: str) -> None:
        resolver({"public.example.com": [ip]})
        # Must not raise.
        assert_public_host("public.example.com", port=993)

    def test_public_ipv6_allowed(self, prod_env: None, resolver) -> None:
        # 2001:4860:: is a Google global address.
        resolver({"v6public.example.com": ["2001:4860:4860::8888"]})
        assert_public_host("v6public.example.com", port=993)

    def test_one_private_alongside_public_still_blocks(self, prod_env: None, resolver) -> None:
        """If a host has BOTH a public and a private A-record, we still
        block — an attacker could otherwise win the race by getting the
        private one picked first.
        """
        resolver({"mixed.example.com": ["8.8.8.8", "10.0.0.1"]})
        with pytest.raises(InvalidHostError):
            assert_public_host("mixed.example.com", port=993)


# ---------------------------------------------------------------------------
# Resolver failures
# ---------------------------------------------------------------------------


class TestResolverFailure:
    def test_unresolvable_host_in_prod_raises_invalid_host(self, prod_env: None, resolver) -> None:
        resolver({})  # empty mapping -> gaierror for any lookup
        with pytest.raises(InvalidHostError) as ei:
            assert_public_host("nope.example.invalid", port=993)
        assert ei.value.code == "invalid_host"


# ---------------------------------------------------------------------------
# Input validation (runs in any env)
# ---------------------------------------------------------------------------


class TestInputValidation:
    def test_empty_host_rejected(self, dev_env: None) -> None:
        with pytest.raises(InvalidHostError):
            assert_public_host("", port=993)

    @pytest.mark.parametrize("port", [0, -1, 65536, 100000])
    def test_invalid_port_rejected(self, dev_env: None, port: int) -> None:
        with pytest.raises(InvalidHostError):
            assert_public_host("any.example.com", port=port)

    @pytest.mark.parametrize("port", [1, 25, 587, 993, 65535])
    def test_valid_ports_accepted(self, dev_env: None, port: int) -> None:
        # Dev bypass + valid port = no exception.
        assert_public_host("any.example.com", port=port)
