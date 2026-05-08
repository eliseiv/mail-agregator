"""MailAccount model — IMAP+SMTP credentials for one external mailbox.

DDL contract: ``docs/03-data-model.md`` table ``mail_accounts``.

Encrypted password format: see ADR-0005 / ``shared.crypto``.
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


class MailAccount(Base):
    __tablename__ = "mail_accounts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    email: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    encrypted_password: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    imap_host: Mapped[str] = mapped_column(Text, nullable=False)
    imap_port: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("993"))
    imap_ssl: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    smtp_host: Mapped[str] = mapped_column(Text, nullable=False)
    smtp_port: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("465"))
    smtp_ssl: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    smtp_starttls: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    smtp_username: Mapped[str | None] = mapped_column(Text, nullable=True)
    smtp_encrypted_password: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    last_synced_uidnext: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    last_uidvalidity: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_sync_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        CheckConstraint("imap_port BETWEEN 1 AND 65535", name="ck_mail_accounts_imap_port"),
        CheckConstraint("smtp_port BETWEEN 1 AND 65535", name="ck_mail_accounts_smtp_port"),
        CheckConstraint("NOT (smtp_ssl AND smtp_starttls)", name="ck_mail_accounts_smtp_ssl_xor"),
        CheckConstraint(
            "display_name IS NULL OR char_length(display_name) BETWEEN 1 AND 100",
            name="ck_mail_accounts_display_name_length",
        ),
        UniqueConstraint("user_id", "email", name="uq_mail_accounts_user_email"),
        Index("ix_mail_accounts_user_id", "user_id"),
        Index(
            "ix_mail_accounts_active_partial",
            "is_active",
            postgresql_where=text("is_active = true"),
        ),
    )
