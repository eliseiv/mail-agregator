"""GroupForwarding ORM (ADR-0034 §1.1).

DDL contract: ``docs/03-data-model.md`` table ``group_forwarding``.

- ``GroupForwarding`` — one row per team (``UNIQUE(group_id)``), the
  leader's forward-to address plus an ``is_active`` toggle. Fork of
  ``webhooks`` **without** a secret (forwarding sends through the mailbox's
  own SMTP credentials, so no external-receiver auth is needed).

``created_at`` is the "don't flood history" anchor: the dispatcher only
forwards messages whose ``internal_date >= created_at`` (ADR-0034 §3.4).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from shared.db import Base


class GroupForwarding(Base):
    """Mail-forwarding configuration for one team (group)."""

    __tablename__ = "group_forwarding"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("groups.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    # Destination e-mail (the leader's address). Plaintext — it is not a
    # secret. Format validated at the API boundary (manual pattern, see
    # ``backend/app/forwarding/schemas.py``).
    forward_to: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
