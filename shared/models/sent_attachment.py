"""SentAttachment — placeholder for the not-yet-shipped attachment-on-send UX.

The table is created empty per ``docs/03-data-model.md`` so we can ship the
feature later without a schema migration. UI does not expose upload yet
(TD-005).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from shared.db import Base


class SentAttachment(Base):
    __tablename__ = "sent_attachments"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    sent_message_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("sent_messages.id", ondelete="CASCADE"),
        nullable=False,
    )
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    content_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    s3_key: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
