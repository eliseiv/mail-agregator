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
