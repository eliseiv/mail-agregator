"""ADR-0047 §5 — env bounds of ``MAILBOX_TEST_DEADLINE_SECONDS``.

Source of truth: ``docs/adr/ADR-0047-mailbox-test-hard-deadline.md`` §5 and
``docs/05-modules.md`` §9.2 «Конфигурация» — ``Field(default=45, ge=10, le=45)``.

``le`` is a MACHINE guard of the §2.1 invariant, not decoration:

    le  <=  proxy_read_timeout (60)  -  teardown (5)  -  non-probe (5)  -  reserve (5)  =  45

The amendment tightened ``le`` from ``50`` to ``45`` (TD-058): at ``50`` the worst
case is exactly ``50 + 5 + 5 = 60`` — nginx's ``proxy_read_timeout`` — i.e. the
very ``504`` HTML (instead of the domain ``422``) the ADR exists to prevent. So
``46`` MUST be rejected; ``45`` and ``10`` MUST be accepted; ``9`` MUST be
rejected.

We instantiate :class:`Settings` directly: pydantic-settings gives init kwargs
the highest priority, so the ambient ``.env`` cannot mask the value under test
(same pattern as ``test_config_adr0029.py``).
"""

from __future__ import annotations

import base64

import pytest
from pydantic import ValidationError

from shared.config import Settings

pytestmark = pytest.mark.unit

_VALID_KEY = base64.b64encode(b"\x00" * 32).decode()

_REQUIRED = {
    "MAIL_ENCRYPTION_KEY": _VALID_KEY,
    "ADMIN_PASSWORD": "x",
    "S3_ACCESS_KEY": "x",
    "S3_SECRET_KEY": "x",
}


def _settings(**overrides: object) -> Settings:
    return Settings(**{**_REQUIRED, **overrides})  # type: ignore[arg-type]


class TestDefault:
    def test_deadline_defaults_to_45(self) -> None:
        assert _settings().MAILBOX_TEST_DEADLINE_SECONDS == 45


class TestUpperBound:
    def test_45_is_accepted(self) -> None:
        assert _settings(MAILBOX_TEST_DEADLINE_SECONDS=45).MAILBOX_TEST_DEADLINE_SECONDS == 45

    def test_46_is_rejected(self) -> None:
        """One second above the bound: ``46 + 5 + 5 = 56`` leaves no reserve (§5)."""
        with pytest.raises(ValidationError):
            _settings(MAILBOX_TEST_DEADLINE_SECONDS=46)

    def test_50_is_rejected_after_the_teardown_amendment(self) -> None:
        """The value the OLD ``le=50`` allowed — worst case exactly 60 s → ``504``."""
        with pytest.raises(ValidationError):
            _settings(MAILBOX_TEST_DEADLINE_SECONDS=50)


class TestLowerBound:
    def test_10_is_accepted(self) -> None:
        assert _settings(MAILBOX_TEST_DEADLINE_SECONDS=10).MAILBOX_TEST_DEADLINE_SECONDS == 10

    def test_9_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _settings(MAILBOX_TEST_DEADLINE_SECONDS=9)
