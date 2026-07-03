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
    # FE-FIX round-10: group_id is the visibility key for non-super_admin
    # callers. Set on insert from the owner's current ``users.group_id``;
    # NULL means "personal" (visible to the owner + super_admin only).
    # ON DELETE SET NULL keeps the account when its group is removed.
    group_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("groups.id", ondelete="SET NULL"),
        nullable=True,
    )
    email: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    # ADR-0025: ``encrypted_password`` is now NULLABLE — oauth_outlook
    # accounts authenticate via XOAUTH2 tokens and have no password. The
    # CHECK ``ck_mail_accounts_password_creds`` keeps it NOT NULL for
    # ``auth_type='password'`` rows.
    encrypted_password: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    # --- ADR-0025: OAuth2 (XOAUTH2) for personal Outlook accounts ---------
    # ``password`` = IMAP/SMTP LOGIN with ``encrypted_password``;
    # ``oauth_outlook`` = SASL XOAUTH2 with the oauth_* token columns below.
    auth_type: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'password'"))
    oauth_provider: Mapped[str | None] = mapped_column(Text, nullable=True)
    # AES-256-GCM blobs (shared.crypto.MailPasswordCipher, AAD=account_id).
    oauth_refresh_token_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    oauth_access_token_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    oauth_access_token_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    oauth_needs_consent: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    oauth_scopes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Reserved per-account proxy (ADR-0025 §1, TD-029) — NOT used yet.
    proxy_url: Mapped[str | None] = mapped_column(Text, nullable=True)
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
    # ADR-0033: idempotency stamp for the "mailbox auto-disabled" Telegram
    # alert. NULL = no outstanding alert (normal state of an active mailbox).
    # Set ``= now()`` guarded (``WHERE disabled_alert_sent_at IS NULL``) in the
    # same transaction as ``is_active=false`` inside
    # ``worker.sync_cycle._disable_after_failures`` — one alert per
    # Active→Disabled transition. Reset to NULL on re-enable
    # (``MailAccountService.update`` creds-changed branch). Migration
    # ``20260703_020``.
    disabled_alert_sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
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
        # ADR-0025 — auth_type domain + per-type credential invariants.
        CheckConstraint(
            "auth_type IN ('password', 'oauth_outlook')",
            name="ck_mail_accounts_auth_type",
        ),
        CheckConstraint(
            "auth_type <> 'password' OR encrypted_password IS NOT NULL",
            name="ck_mail_accounts_password_creds",
        ),
        CheckConstraint(
            "auth_type <> 'oauth_outlook' "
            "OR (oauth_refresh_token_encrypted IS NOT NULL AND oauth_provider = 'outlook')",
            name="ck_mail_accounts_oauth_creds",
        ),
        UniqueConstraint("user_id", "email", name="uq_mail_accounts_user_email"),
        Index("ix_mail_accounts_user_id", "user_id"),
        Index(
            "ix_mail_accounts_active_partial",
            "is_active",
            postgresql_where=text("is_active = true"),
        ),
    )
