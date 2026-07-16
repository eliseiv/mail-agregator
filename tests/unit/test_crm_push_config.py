"""Unit tests for CRM-push gating + secret redaction (ADR-0043 §2).

``crm_push_enabled`` / ``crm_status_enabled`` are derived flags: when
``CRM_INGEST_URL`` / ``CRM_MAILBOX_STATUS_URL`` / ``CRM_PUSH_SECRET`` are empty the
matching jobs are NOT registered and ``sync_cycle`` does NOT enqueue.
``CRM_PUSH_SECRET`` is on the redact-list so its value never reaches a log line.

Settings are built hermetically (``Settings(**_REQUIRED, **overrides)``) WITHOUT reading
the process environment / local ``.env`` — otherwise real values would give false results.
"""

from __future__ import annotations

import pytest

from shared.config import Settings
from shared.logging import REDACT_KEYS, _redact_processor

pytestmark = pytest.mark.unit

_VALID_KEY = "HSoYMcwRZLguwQpz+kHPwifN9LvO/H86royMLyRgclo="
_REQUIRED = {
    "MAIL_ENCRYPTION_KEY": _VALID_KEY,
}


def _settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **{**_REQUIRED, **overrides})  # type: ignore[arg-type]


# --------------------------------------------------------------- crm_push_enabled
def test_push_disabled_when_config_empty() -> None:
    assert _settings().crm_push_enabled is False


def test_push_disabled_with_only_url() -> None:
    assert _settings(CRM_INGEST_URL="https://crm.example").crm_push_enabled is False


def test_push_disabled_with_only_secret() -> None:
    assert _settings(CRM_PUSH_SECRET="s").crm_push_enabled is False


def test_push_enabled_with_both() -> None:
    s = _settings(CRM_INGEST_URL="https://crm.example", CRM_PUSH_SECRET="s")
    assert s.crm_push_enabled is True


# ------------------------------------------------------------- crm_status_enabled
def test_status_disabled_when_config_empty() -> None:
    assert _settings().crm_status_enabled is False


def test_status_disabled_with_only_url() -> None:
    assert _settings(CRM_MAILBOX_STATUS_URL="https://crm.example").crm_status_enabled is False


def test_status_enabled_with_both() -> None:
    s = _settings(CRM_MAILBOX_STATUS_URL="https://crm.example", CRM_PUSH_SECRET="s")
    assert s.crm_status_enabled is True


# --------------------------------------------------------- secret is not logged
def test_crm_push_secret_in_redact_list() -> None:
    assert "CRM_PUSH_SECRET" in REDACT_KEYS


def test_redact_processor_masks_crm_push_secret() -> None:
    event = {"event": "crm_push", "CRM_PUSH_SECRET": "super-secret-value"}
    redacted = _redact_processor(None, "info", event)  # type: ignore[arg-type]
    assert redacted["CRM_PUSH_SECRET"] == "[REDACTED]"
    assert "super-secret-value" not in str(redacted)
