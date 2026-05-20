"""Webhook + WebhookDelivery ORM (ADR-0023 §1).

DDL contract: ``docs/03-data-model.md`` tables ``webhooks`` and
``webhook_deliveries``.

- ``Webhook``         — one row per team (``UNIQUE(group_id)``);
  ``secret_encrypted`` is AES-256-GCM with AAD bound to the row id
  (see :mod:`shared.crypto` + ADR-0023 §4.1).
- ``WebhookDelivery`` — idempotency registry; ``UNIQUE(webhook_id,
  message_id)`` lets the dispatcher use ``INSERT ... ON CONFLICT DO
  NOTHING RETURNING id`` to claim ownership of a (webhook, message)
  pair exactly once.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from shared.db import Base


class Webhook(Base):
    """Outbound webhook configuration for one team (group)."""

    __tablename__ = "webhooks"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("groups.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    url: Mapped[str] = mapped_column(Text, nullable=False)
    secret_encrypted: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    consecutive_failures: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    dead_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_fired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        CheckConstraint("url LIKE 'https://%'", name="webhooks_url_https_check"),
        CheckConstraint("char_length(url) BETWEEN 9 AND 2048", name="webhooks_url_length_check"),
        Index(
            "webhooks_active_idx",
            "is_active",
            postgresql_where=text("is_active = true"),
        ),
    )


class WebhookDelivery(Base):
    """Idempotency registry: one row per delivered (or attempted) event."""

    __tablename__ = "webhook_deliveries"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    webhook_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("webhooks.id", ondelete="CASCADE"),
        nullable=False,
    )
    message_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("messages.id", ondelete="CASCADE"),
        nullable=False,
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    response_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_excerpt: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        UniqueConstraint("webhook_id", "message_id", name="webhook_deliveries_unique"),
        Index("webhook_deliveries_webhook_id_idx", "webhook_id"),
        Index("webhook_deliveries_message_id_idx", "message_id"),
    )
