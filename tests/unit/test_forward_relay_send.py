"""Unit tests for the forward SMTP relay send helper (ADR-0034 §5 relay branch).

Source of truth: ``backend/app/send/service.py::smtp_send_via_relay``. The
helper is verified in isolation — ``aiosmtplib.send``, the module-level
``get_settings`` and ``assert_public_host`` (SSRF check) are all mocked, so no
DB / network / real settings are needed. Covered: the relay credentials +
TLS knobs + recipients are forwarded to ``aiosmtplib.send``; and the error
matrix (SMTP / timeout / OS errors) maps to ``SMTPSendFailedError`` with the
host detail stripped.
"""

from __future__ import annotations

from email.message import EmailMessage
from types import SimpleNamespace
from typing import Any

import aiosmtplib
import pytest

from backend.app.exceptions import SMTPSendFailedError
from backend.app.send import service as snd_svc

pytestmark = pytest.mark.unit


async def _noop_host_guard(host: str, *, port: int) -> None:
    """Stand-in for the SSRF guard.

    TD-056 / ADR-0047 §4 turned this call-site into the OFF-LOOP
    ``assert_public_host_async`` (``send/service.py:298``): the blocking
    ``getaddrinfo`` of the sync variant used to run in the event-loop thread.
    The sync name is no longer imported by the module, so patching it here was
    a dead mock (``monkeypatch.setattr`` raised ``AttributeError``); the guard
    is now stubbed under its real, awaitable name.
    """


def _stub_relay_settings() -> SimpleNamespace:
    return SimpleNamespace(
        FORWARD_SMTP_HOST="relay.service.example",
        FORWARD_SMTP_PORT=587,
        FORWARD_SMTP_USERNAME="relay-user",
        FORWARD_SMTP_PASSWORD="relay-secret",
        FORWARD_SMTP_SSL=False,
        FORWARD_SMTP_STARTTLS=True,
    )


async def test_relay_send_forwards_relay_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(snd_svc, "get_settings", _stub_relay_settings)
    monkeypatch.setattr(snd_svc, "assert_public_host_async", _noop_host_guard)

    calls: list[dict[str, Any]] = []

    async def _fake_send(msg: Any, **kwargs: Any) -> None:
        calls.append(kwargs)

    monkeypatch.setattr("aiosmtplib.send", _fake_send)

    await snd_svc.smtp_send_via_relay(EmailMessage(), ["leader@company.com"])

    assert len(calls) == 1
    kw = calls[0]
    assert kw["hostname"] == "relay.service.example"
    assert kw["port"] == 587
    assert kw["username"] == "relay-user"
    assert kw["password"] == "relay-secret"
    assert kw["use_tls"] is False
    assert kw["start_tls"] is True
    assert kw["recipients"] == ["leader@company.com"]
    assert kw["timeout"] == snd_svc._SMTP_TIMEOUT


@pytest.mark.parametrize(
    "exc",
    [
        aiosmtplib.SMTPException("relay.service.example said no"),
        TimeoutError("connect timed out"),
        OSError("connection reset"),
    ],
)
async def test_relay_send_wraps_transport_errors(
    monkeypatch: pytest.MonkeyPatch, exc: Exception
) -> None:
    monkeypatch.setattr(snd_svc, "get_settings", _stub_relay_settings)
    monkeypatch.setattr(snd_svc, "assert_public_host_async", _noop_host_guard)

    async def _bad(msg: Any, **kwargs: Any) -> None:
        raise exc

    monkeypatch.setattr("aiosmtplib.send", _bad)

    with pytest.raises(SMTPSendFailedError):
        await snd_svc.smtp_send_via_relay(EmailMessage(), ["leader@company.com"])
