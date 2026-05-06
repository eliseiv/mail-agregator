"""SentMessage — outgoing email metadata. Retention: indefinite (ADR-0011).

DDL contract: ``docs/03-data-model.md`` table ``sent_messages``.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from shared.db import Base


class SentMessage(Base):
    __tablename__ = "sent_messages"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    from_account_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("mail_accounts.id", ondelete="CASCADE"),
        nullable=False,
    )
    to_addrs: Mapped[str] = mapped_column(Text, nullable=False)
    cc_addrs: Mapped[str | None] = mapped_column(Text, nullable=True)
    bcc_addrs: Mapped[str | None] = mapped_column(Text, nullable=True)
    subject: Mapped[str | None] = mapped_column(Text, nullable=True)
    body_text: Mapped[str] = mapped_column(Text, nullable=False)
    in_reply_to: Mapped[str | None] = mapped_column(Text, nullable=True)
    refs_header: Mapped[str | None] = mapped_column(Text, nullable=True)
    smtp_message_id: Mapped[str] = mapped_column(Text, nullable=False)
    appended_to_sent: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    appended_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        Index(
            "ix_sent_messages_user_sent_at_desc",
            "user_id",
            text("sent_at DESC"),
        ),
        Index("ix_sent_messages_from_account", "from_account_id"),
    )
