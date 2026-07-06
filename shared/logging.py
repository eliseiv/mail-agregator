"""Structured JSON logging with structlog (ADR-0014).

Standard fields on every record: ``timestamp``, ``level``, ``event``,
``service``. Optional context: ``request_id`` (api), ``cycle_id`` (worker),
``user_id``, ``mail_account_id``.

Redact-list — values for these keys are replaced with ``[REDACTED]`` even
when callers accidentally pass them. Belt-and-braces: callers are also
forbidden from logging them (ADR-0014).
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog
from structlog.types import EventDict

REDACT_KEYS: frozenset[str] = frozenset(
    {
        "password",
        "new_password",
        "old_password",
        "password_confirm",
        "encrypted_password",
        "smtp_password",
        "smtp_encrypted_password",
        "csrf_token",
        "session_token",
        "setup_token",
        "mas_session",
        "mas_csrf",
        "mas_setup",
        "Authorization",
        "authorization",
        "X-CSRF-Token",
        "MAIL_ENCRYPTION_KEY",
        "MAIL_ENCRYPTION_KEY_PREV",
        "ADMIN_PASSWORD",
        "S3_SECRET_KEY",
        "POSTGRES_PASSWORD",
        # Telegram launcher (ADR-0018, docs/06-security.md §1.8): bot token
        # leak lets attacker impersonate the bot. Operator env var is
        # ``BOT_TOKEN`` (see shared/config.py for naming note); legacy
        # ``TELEGRAM_BOT_TOKEN`` covers any place still using the docs name.
        "BOT_TOKEN",
        "TELEGRAM_BOT_TOKEN",
        # ADR-0027 §8: push-only per-team bot tokens — same leak risk as
        # BOT_TOKEN (attacker could impersonate the bot to the admins).
        "BOT_IVAN_TOKEN",
        "BOT_ALEXANDRA_TOKEN",
        "BOT_ANDREI_TOKEN",
        # ADR-0027 §8 (round-44): fourth push bot ``business2`` token.
        "BOT_BUSINESS2_TOKEN",
        # ADR-0027 §8 (round-42): per-bot push-webhook secrets — same leak
        # risk as TELEGRAM_WEBHOOK_SECRET (forged push-webhook updates).
        "BOT_IVAN_WEBHOOK_SECRET",
        "BOT_ALEXANDRA_WEBHOOK_SECRET",
        "BOT_ANDREI_WEBHOOK_SECRET",
        # ADR-0027 §8 (round-44): business2 push-webhook secret.
        "BOT_BUSINESS2_WEBHOOK_SECRET",
        "TELEGRAM_WEBHOOK_SECRET",
        "X-Telegram-Bot-Api-Secret-Token",
        # OAuth2 Outlook (ADR-0025 §1.11, docs/06-security.md): never log the
        # authorization code, tokens, PKCE verifier or client secret.
        "OUTLOOK_CLIENT_SECRET",
        "client_secret",
        "code",
        "code_verifier",
        "access_token",
        "refresh_token",
        "oauth_access_token",
        "oauth_refresh_token",
        "id_token",
        # External PULL-API (ADR-0029 §Security): the static partner key must
        # never appear in logs in any header / env form. ``Authorization``
        # (Bearer transport) is already covered above.
        "EXTERNAL_API_KEY",
        "X-API-Key",
        "x-api-key",
        # Forward SMTP relay (ADR-0034 §5, docs/06-security.md §1.14): the
        # relay account password lets an attacker send mail as the relay.
        # Same leak class as the mailbox SMTP passwords above.
        "FORWARD_SMTP_PASSWORD",
    }
)


def _redact_processor(_logger: Any, _method_name: str, event_dict: EventDict) -> EventDict:
    """Replace any value whose key matches the redact list."""
    for key in list(event_dict.keys()):
        if key in REDACT_KEYS:
            event_dict[key] = "[REDACTED]"
    return event_dict


def configure_logging(level: str = "INFO", service: str = "api") -> None:
    """Configure structlog + stdlib logging once at process start.

    Idempotent: safe to call multiple times (later calls just re-bind the
    service name).
    """
    log_level = getattr(logging, level.upper(), logging.INFO)

    # Route stdlib loggers (uvicorn, gunicorn, sqlalchemy) through stdout
    # at the configured level so we don't lose their messages.
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=log_level,
        force=True,
    )

    # Quiet down noisy libraries that double-log at DEBUG.
    for noisy in ("uvicorn.access", "asyncio"):
        logging.getLogger(noisy).setLevel(max(log_level, logging.INFO))

    # ``httpx`` logs the full request URL at INFO via its internal logger,
    # which would leak the Telegram Bot token embedded in the api.telegram.org
    # URL (`bot<TOKEN>/<method>`) — see ADR-0018 §6 / docs/06-security.md
    # §1.8. Silence it to WARNING; we still see real failures (4xx/5xx)
    # via our own ``telegram_send_message_api_error`` event in
    # ``backend/app/telegram/bot.py`` and via the redacted-aware
    # ``_redact_processor`` for any structlog event we emit ourselves.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _redact_processor,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(sort_keys=True),
        ],
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        cache_logger_on_first_use=True,
    )

    # Bind service name as a default context var so every log record carries
    # it (api vs worker).
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(service=service)


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a bound logger; ``name`` is conventionally the module name."""
    logger: structlog.stdlib.BoundLogger
    logger = structlog.get_logger(name) if name else structlog.get_logger()
    return logger
