"""Application settings loaded from environment.

Single source of truth for env-driven configuration. Imported by api and
worker. Documented env variables: ``docs/07-deployment.md`` sec. 4.

Invariants (``docs/05-modules.md`` sec. 1):
- Missing required env -> process aborts with a clear message at startup.
- ``MAIL_ENCRYPTION_KEY`` is base64; decoded length MUST equal 32 bytes.
- In ``APP_ENV=prod`` we hard-disable ``ENABLE_DOCS`` regardless of env.
- ``get_settings()`` is a singleton (``lru_cache``).
- Nothing here is logged (see redact-list in ``shared/logging.py``).
"""

from __future__ import annotations

import base64
from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

AppEnv = Literal["dev", "prod"]


class Settings(BaseSettings):
    """Process-wide configuration.

    All values are sourced from environment variables. ``.env`` is read
    when present (dev convenience) but production injects via ``docker
    compose`` env_file.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # --- General ---
    APP_ENV: AppEnv = "prod"
    APP_BASE_URL: str = "https://mail.example.com"
    LOG_LEVEL: str = "INFO"
    ENABLE_DOCS: bool = False
    SERVICE_NAME: str = "api"  # overridden to "worker" by worker.app.main

    # --- Database ---
    DATABASE_URL: str = "postgresql+asyncpg://mas:CHANGE_ME@postgres:5432/mail_aggregator"

    # --- Redis ---
    REDIS_URL: str = "redis://redis:6379/0"

    # --- MinIO / S3 ---
    S3_ENDPOINT_URL: str = "http://minio:9000"
    S3_ACCESS_KEY: str = ""
    S3_SECRET_KEY: str = ""
    S3_BUCKET_NAME: str = "mail-attachments"
    S3_REGION: str = "us-east-1"

    # --- Crypto (mail account passwords, AES-256-GCM, ADR-0005) ---
    MAIL_ENCRYPTION_KEY: str = ""  # base64 of exactly 32 raw bytes
    MAIL_ENCRYPTION_KEY_PREV: str | None = None  # only during rotation

    # --- Admin seed ---
    ADMIN_LOGIN: str = "admin"
    ADMIN_PASSWORD: str = ""

    # --- Worker / sync ---
    MAX_CONCURRENT_IMAP: int = Field(default=10, ge=1, le=100)
    WORKER_THREAD_POOL_SIZE: int = Field(default=14, ge=1, le=200)
    SYNC_INTERVAL_MINUTES: int = Field(default=5, ge=1, le=60)
    RETENTION_DAYS: int = Field(default=30, ge=1, le=3650)
    IMAP_TIMEOUT_SECONDS: int = Field(default=60, ge=5, le=600)
    INITIAL_SYNC_DAYS: int = Field(default=30, ge=1, le=365)
    MAX_ATTACHMENT_BYTES: int = Field(default=26_214_400, ge=1024, le=1_073_741_824)
    MAX_BODY_BYTES: int = Field(default=1_048_576, ge=1024, le=10_485_760)

    # --- Sessions / auth ---
    SESSION_TTL_SECONDS: int = Field(default=43_200, ge=60)
    SESSION_ABSOLUTE_TTL_SECONDS: int = Field(default=604_800, ge=60)
    SETUP_SESSION_TTL_SECONDS: int = Field(default=900, ge=60)
    COOKIE_DOMAIN: str | None = None
    LOGIN_FAILURE_THRESHOLD: int = Field(default=5, ge=1, le=100)
    LOGIN_LOCKOUT_MINUTES: int = Field(default=15, ge=1, le=1440)

    # --- HTTP ---
    SAFE_REDIRECT_AFTER_LOGIN: str = "/"
    LOGIN_PATH: str = "/login"

    # --- Telegram launcher bot (ADR-0018) + persistent SSO / notifications
    #     (ADR-0022) ---
    # NOTE: ADR-0018 + docs/05-modules.md sec. 18 reference the env name
    # ``TELEGRAM_BOT_TOKEN``. Operator deployment uses ``BOT_TOKEN`` (it is
    # what BotFather copy-paste UX surfaces and what is already provisioned
    # in the prod ``.env``); rename in docs is tracked separately as a
    # documentation-consistency item — code here uses ``BOT_TOKEN`` as the
    # source of truth. Marked redact in ``shared/logging.py``.
    BOT_TOKEN: str = ""
    TELEGRAM_WEBHOOK_SECRET: str = ""
    TELEGRAM_WEBAPP_URL: str = ""

    # --- Telegram persistent SSO (ADR-0022 §1) ---
    # TTL for ``initData.auth_date`` — 5 minutes (jSO ADR-0022 §1.2).
    TG_AUTH_INIT_DATA_TTL_SECONDS: int = Field(default=300, ge=30, le=86_400)
    # TTL of ``mas_tg_pending`` cookie / Redis token — 15 minutes.
    TG_PENDING_LINK_TTL_SECONDS: int = Field(default=900, ge=60, le=86_400)
    # ADR-0024: soft cap on the number of active Telegram links one internal
    # user may hold (personal / work / …). Application-level (checked via
    # COUNT(*) in link_pending), not a DB constraint — see ADR-0024 §1/§3.
    TG_MAX_LINKS_PER_USER: int = Field(default=10, ge=1, le=100)

    # --- Telegram push-notifications dispatcher (ADR-0022 §2.4 + §2.8) ---
    # How often the dispatcher drains the Redis ``tg_notify_queue``.
    TG_NOTIFY_DISPATCH_INTERVAL_SECONDS: int = Field(default=5, ge=1, le=600)
    # Recovery scan cadence — finds messages with tags but no notification row.
    TG_NOTIFY_RECOVERY_INTERVAL_SECONDS: int = Field(default=3600, ge=60, le=86_400)
    # Lookback window for recovery scan.
    TG_NOTIFY_RECOVERY_WINDOW_HOURS: int = Field(default=24, ge=1, le=720)
    # Per-tick LPOP batch size (cap on dispatcher throughput / tick).
    TG_NOTIFY_BATCH_SIZE: int = Field(default=30, ge=1, le=500)
    # Per-tick recovery LPUSH batch size.
    TG_NOTIFY_RECOVERY_BATCH_SIZE: int = Field(default=200, ge=1, le=10_000)
    # Round-31 (ADR-0022 §2.1): notify about EVERY new message, not only
    # tagged ones. ``true`` (default) — every inserted message is enqueued;
    # ``false`` — historical behaviour (only messages with >=1 tag). Toggling
    # to ``false`` reverts without a code redeploy (worker restart only, so
    # ``get_settings()`` lru-cache re-reads env).
    TG_NOTIFY_ALL_MESSAGES: bool = Field(default=True)
    # Round-31 (ADR-0022 §2.9): per-chat send throttle (msg / minute / chat).
    # Caps Telegram Bot API pressure per chat_id; over-limit recipients are
    # skipped this tick and picked up later by the recovery scan.
    TG_SEND_PER_CHAT_PER_MINUTE: int = Field(default=20, ge=1, le=60)

    # --- Outlook OAuth2 (ADR-0025, Sprint B) ------------------------------
    # Azure App (Personal Microsoft accounts only) credentials. When BOTH
    # client id and secret are set, ``outlook_oauth_enabled`` flips on and the
    # ``/api/oauth/outlook/*`` routes serve real flows; otherwise they 404
    # (route hidden — symmetric to ``telegram_bot_enabled``).
    OUTLOOK_CLIENT_ID: str = ""
    # Marked redact in ``shared/logging.py`` (alongside MAIL_ENCRYPTION_KEY).
    OUTLOOK_CLIENT_SECRET: str = ""
    # ``{APP_BASE_URL}/api/oauth/outlook/callback`` — must match Azure exactly.
    OUTLOOK_REDIRECT_URI: str = ""
    # tenant for the authorize/token endpoints; personal mailboxes -> consumers.
    OUTLOOK_TENANT: str = "consumers"
    # TTL of the Redis ``oauth_state:{state}`` key (CSRF/anti-fixation state +
    # PKCE verifier). Default 600s per ADR-0025 §6.
    OUTLOOK_OAUTH_STATE_TTL_SECONDS: int = Field(default=600, ge=60, le=3600)

    # --- Outbound webhooks dispatcher (ADR-0023 §3 + §5) ------------------
    # How often ``webhook_dispatch`` drains the Redis queue.
    WEBHOOK_DISPATCH_INTERVAL_SECONDS: int = Field(default=5, ge=1, le=600)
    # ``webhook_recovery_scan`` cadence — finds tagged messages without a
    # successful ``webhook_deliveries`` row in the lookback window.
    WEBHOOK_RECOVERY_INTERVAL_SECONDS: int = Field(default=3600, ge=60, le=86_400)
    # Lookback window for the recovery scan.
    WEBHOOK_RECOVERY_WINDOW_HOURS: int = Field(default=24, ge=1, le=720)
    # Per-tick LPOP batch size (caps throughput per dispatcher tick).
    WEBHOOK_BATCH_SIZE: int = Field(default=30, ge=1, le=500)
    # Per-tick recovery LPUSH batch size.
    WEBHOOK_RECOVERY_BATCH_SIZE: int = Field(default=200, ge=1, le=10_000)
    # Total httpx timeout (connect+read+write) for outbound POST.
    WEBHOOK_HTTP_TIMEOUT_SECONDS: int = Field(default=10, ge=1, le=120)
    # Mark dead after this many consecutive non-retriable 4xx responses.
    WEBHOOK_MAX_FAILURES_BEFORE_DEAD: int = Field(default=10, ge=1, le=1000)
    # POST ``/api/webhooks/me/test`` rate-limit (per webhook, per hour).
    WEBHOOK_TEST_LIMIT: int = Field(default=10, ge=1, le=1000)

    @field_validator("MAIL_ENCRYPTION_KEY")
    @classmethod
    def _validate_master_key(cls, v: str) -> str:
        """Reject anything that doesn't decode to exactly 32 bytes."""
        if not v:
            return v  # validated again in model_validator if required
        try:
            raw = base64.b64decode(v, validate=True)
        except Exception as exc:  # - want to wrap any decode error
            raise ValueError("MAIL_ENCRYPTION_KEY: not valid base64") from exc
        if len(raw) != 32:
            raise ValueError(f"MAIL_ENCRYPTION_KEY: must decode to 32 bytes, got {len(raw)}")
        return v

    @field_validator("MAIL_ENCRYPTION_KEY_PREV")
    @classmethod
    def _validate_master_key_prev(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return None
        try:
            raw = base64.b64decode(v, validate=True)
        except Exception as exc:
            raise ValueError("MAIL_ENCRYPTION_KEY_PREV: not valid base64") from exc
        if len(raw) != 32:
            raise ValueError(f"MAIL_ENCRYPTION_KEY_PREV: must decode to 32 bytes, got {len(raw)}")
        return v

    @model_validator(mode="after")
    def _enforce_required(self) -> Settings:
        """Required-in-prod env values; harden ENABLE_DOCS in prod."""
        missing: list[str] = []
        if not self.MAIL_ENCRYPTION_KEY:
            missing.append("MAIL_ENCRYPTION_KEY")
        if not self.ADMIN_PASSWORD:
            missing.append("ADMIN_PASSWORD")
        if not self.S3_ACCESS_KEY:
            missing.append("S3_ACCESS_KEY")
        if not self.S3_SECRET_KEY:
            missing.append("S3_SECRET_KEY")
        if missing:
            raise ValueError("Missing required env: " + ", ".join(missing))

        # Hardcoded prod policy: docs disabled regardless of env value.
        if self.APP_ENV == "prod":
            object.__setattr__(self, "ENABLE_DOCS", False)
        return self

    @property
    def is_prod(self) -> bool:
        return self.APP_ENV == "prod"

    @property
    def cookie_secure(self) -> bool:
        """Set-Cookie ``Secure`` flag — only in prod (TLS terminated upstream)."""
        return self.is_prod

    @property
    def telegram_bot_enabled(self) -> bool:
        """True only when *all* required Telegram env vars are populated.

        Per ADR-0018 §6: webhook route is always registered, but if any of
        the three required values is empty it returns 200 OK without
        contacting the Bot API. Lets us wire the route in dev/CI without
        BotFather setup.
        """
        return bool(self.BOT_TOKEN and self.TELEGRAM_WEBHOOK_SECRET and self.TELEGRAM_WEBAPP_URL)

    @property
    def outlook_oauth_enabled(self) -> bool:
        """True only when both Azure App credentials are configured (ADR-0025 §6).

        Derived flag — symmetric with :pyattr:`telegram_bot_enabled`. When
        false the ``/api/oauth/outlook/*`` routes return 404 (feature hidden);
        lets us wire the routes in dev/CI without a real Azure App.
        """
        return bool(self.OUTLOOK_CLIENT_ID and self.OUTLOOK_CLIENT_SECRET)

    @property
    def outlook_authorize_endpoint(self) -> str:
        """Microsoft authorize endpoint for the configured tenant (ADR-0025 §6)."""
        return f"https://login.microsoftonline.com/{self.OUTLOOK_TENANT}/oauth2/v2.0/authorize"

    @property
    def outlook_token_endpoint(self) -> str:
        """Microsoft token endpoint for the configured tenant (ADR-0025 §6)."""
        return f"https://login.microsoftonline.com/{self.OUTLOOK_TENANT}/oauth2/v2.0/token"

    def mail_master_key_bytes(self) -> bytes:
        """Decoded current key — never logged, never cached on disk."""
        return base64.b64decode(self.MAIL_ENCRYPTION_KEY)

    def mail_master_key_prev_bytes(self) -> bytes | None:
        if not self.MAIL_ENCRYPTION_KEY_PREV:
            return None
        return base64.b64decode(self.MAIL_ENCRYPTION_KEY_PREV)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached singleton so that repeated calls don't re-parse env."""
    return Settings()
