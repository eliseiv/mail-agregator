"""Message model — cached incoming email metadata + plain-text body.

DDL contract: ``docs/03-data-model.md`` table ``messages``.

Idempotency: ``UNIQUE (mail_account_id, uidvalidity, uid)`` — re-running
sync never duplicates messages even on race / restart.
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
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from shared.db import Base


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    mail_account_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("mail_accounts.id", ondelete="CASCADE"),
        nullable=False,
    )
    uid: Mapped[int] = mapped_column(BigInteger, nullable=False)
    uidvalidity: Mapped[int] = mapped_column(BigInteger, nullable=False)
    message_id_header: Mapped[str | None] = mapped_column(Text, nullable=True)
    from_addr: Mapped[str] = mapped_column(Text, nullable=False)
    from_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    to_addrs: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    cc_addrs: Mapped[str | None] = mapped_column(Text, nullable=True)
    subject: Mapped[str | None] = mapped_column(Text, nullable=True)
    internal_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    body_text: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    # Round-12 bug B: sanitised HTML body (whitelist via bleach). NULL when
    # the source email had no ``text/html`` part (legacy rows are NULL too).
    # Rendered as ``{{ ... | safe }}`` because the sanitiser already
    # stripped scripts / event handlers / ``javascript:`` URLs.
    body_html: Mapped[str | None] = mapped_column(Text, nullable=True)
    body_truncated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    body_present: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    is_read: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    in_reply_to: Mapped[str | None] = mapped_column(Text, nullable=True)
    refs_header: Mapped[str | None] = mapped_column(Text, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    # ADR-0043 §2: push-outbox marker. NULL = not yet delivered to the CRM
    # (`POST {CRM_INGEST_URL}/api/mail/ingest`); set ``= now()`` once the CRM
    # accepts the message (2xx). The ``crm_push_recovery`` scan re-enqueues
    # rows that are still NULL within the retention window. Migration
    # ``20260710_024``.
    pushed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "mail_account_id",
            "uidvalidity",
            "uid",
            name="uq_messages_account_uidv_uid",
        ),
        Index(
            "ix_messages_account_internal_date_desc",
            "mail_account_id",
            text("internal_date DESC"),
        ),
        Index(
            "ix_messages_unread_partial",
            "mail_account_id",
            "is_read",
            postgresql_where=text("is_read = false"),
        ),
        Index("ix_messages_internal_date", "internal_date"),
        # ADR-0043 §2: partial index over undelivered rows for the CRM
        # push-outbox recovery scan (``crm_push_recovery``).
        Index(
            "ix_messages_pushed_at_pending",
            "id",
            postgresql_where=text("pushed_at IS NULL"),
        ),
    )
