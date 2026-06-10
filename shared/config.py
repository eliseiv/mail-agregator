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
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

AppEnv = Literal["dev", "prod"]


@dataclass(frozen=True, slots=True)
class PushTeamBot:
    """A single configured push-only per-team Telegram bot (ADR-0027 §1/§2).

    ``name`` is the team label (``ivan`` / ``alexandra`` / ``andrei``),
    ``token`` the BotFather token used to call the Bot API, ``group_id`` the
    team's ``mail_accounts.group_id`` (ADR-0019) this bot is bound to. Only
    fully-configured bots (non-empty token AND ``group_id > 0``) are
    materialised — see :pyattr:`Settings.push_team_bots`.

    ``webhook_secret`` (round-42, ADR-0027 §2/§10) is the per-bot secret that
    authenticates the push-webhook (``X-Telegram-Bot-Api-Secret-Token`` header)
    and gates the «Посмотреть сообщение» callback button. It may be ``""``:
    the bot still delivers notifications (worker ignores this field), but
    without a button (the callback would have no webhook to land on — see
    :pyattr:`Settings.push_team_bots` / ``worker.app.push_notify_dispatch``).
    """

    name: str
    token: str
    group_id: int
    webhook_secret: str


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
    # --- Sync error resilience (ADR-0026) ---
    # Consecutive PERMANENT failures before auto-disable. Replaces the
    # hard-coded ``_DISABLE_AFTER_FAILS`` in ``worker/app/sync_cycle.py``.
    SYNC_MAX_CONSECUTIVE_FAILURES: int = Field(default=3, ge=1, le=20)
    # Circuit-breaker: per-cycle share of PERMANENT failures at which the mass
    # disable is suppressed (probable common infra outage, not 85 passwords
    # expiring at once). ADR-0026 §3.
    SYNC_MASS_FAILURE_RATIO: float = Field(default=0.5, ge=0.0, le=1.0)
    # Circuit-breaker: minimum accounts processed in a cycle for the breaker to
    # be considered (below this, single-account force-sync behaves normally).
    SYNC_MASS_FAILURE_MIN: int = Field(default=5, ge=1, le=10_000)
    # DNS/connect retries on opening the IMAP connection + login. 0 disables.
    # Backoff 0.5s/1.0s/2.0s; retried for gaierror/connection/network OSError
    # AND sporadic transient IMAP errors ("authenticated but not connected" /
    # "not connected" / "try again" / "temporarily" / "too many"), never for
    # timeouts or real auth/permanent failures. ADR-0026 §4.
    # Default raised 2 -> 3 (ADR-0026 update): Microsoft personal Outlook IMAP
    # sporadically returns "User is authenticated but not connected" on a
    # healthy mailbox; a 3rd attempt clears the flake (backoff 0.5/1.0/2.0).
    SYNC_CONNECT_RETRIES: int = Field(default=3, ge=0, le=10)
    # Suppress a TRANSIENT ``last_sync_error`` write when the last SUCCESSFUL
    # sync was within this many minutes (ADR-0026 update §2): a sporadic flake
    # on an otherwise-working mailbox must not surface in the UI and scare the
    # user. If ``last_synced_at`` is older than this window (or NULL) the
    # transient error IS written (the sync is genuinely stuck). The
    # consecutive-failures counter is never touched either way.
    SYNC_TRANSIENT_SUPPRESS_MINUTES: int = Field(default=60, ge=0, le=10_080)

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

    # --- Push-only per-team Telegram bots (ADR-0027) ----------------------
    # Three additional push-only bots (ivan / alexandra / andrei); each
    # delivers notifications about ALL messages of ITS team (bound by an
    # explicit ``group_id``) to the fixed ``ADMIN_TELEGRAM_IDS``. Worker
    # container reads these; the main bot (``BOT_TOKEN``) is unaffected.
    # Tokens are marked redact in ``shared/logging.py`` alongside BOT_TOKEN.
    # A bot is "configured" only when its token is non-empty AND its
    # group_id > 0 (see ``push_team_bots``). Prod mapping (ADR-0027 §1):
    # ivan=1, alexandra=2, andrei=3.
    BOT_IVAN_TOKEN: str = ""
    BOT_IVAN_GROUP_ID: int = Field(default=0, ge=0)
    # round-42 (ADR-0027 §2/§10): per-bot webhook secret (32 hex,
    # ``openssl rand -hex 16``). Authenticates the push-webhook header and
    # enables the callback button. Empty -> bot delivers without a button.
    # Marked redact in ``shared/logging.py`` alongside the tokens.
    BOT_IVAN_WEBHOOK_SECRET: str = ""
    BOT_ALEXANDRA_TOKEN: str = ""
    BOT_ALEXANDRA_GROUP_ID: int = Field(default=0, ge=0)
    BOT_ALEXANDRA_WEBHOOK_SECRET: str = ""
    BOT_ANDREI_TOKEN: str = ""
    BOT_ANDREI_GROUP_ID: int = Field(default=0, ge=0)
    BOT_ANDREI_WEBHOOK_SECRET: str = ""
    # CSV of the two fixed administrator Telegram chat ids that every
    # push-bot delivers to (e.g. ``11111111,22222222``). Not a secret
    # (chat ids), but parsed defensively — see ``admin_telegram_ids``.
    ADMIN_TELEGRAM_IDS: str = ""
    # How often ``push_notify_dispatch`` drains the Redis ``push_notify_queue``.
    PUSH_NOTIFY_DISPATCH_INTERVAL_SECONDS: int = Field(default=5, ge=1, le=600)
    # Per-tick LPOP batch size (cap on push dispatcher throughput / tick).
    PUSH_NOTIFY_BATCH_SIZE: int = Field(default=30, ge=1, le=500)

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
    # tenant for the authorize/token endpoints. ADR-0025: ``consumers`` for
    # personal mailboxes — this is the working Sprint-B configuration that
    # synced personal Outlook IMAP locally. The P1 switch to ``common`` did NOT
    # fix the prod "User is authenticated but not connected" symptom and has been
    # reverted. Env overrides.
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

        # ADR-0027 §2 invariant: one group_id must not be bound to two
        # different push bots — otherwise a team's mail would ship twice
        # (once per bot). Fail-fast at startup; only CONFIGURED bots (token
        # set AND group_id > 0) participate. Built inline (the property is
        # not yet available during validation, and it filters identically).
        configured_group_ids: list[int] = []
        for token, group_id in (
            (self.BOT_IVAN_TOKEN, self.BOT_IVAN_GROUP_ID),
            (self.BOT_ALEXANDRA_TOKEN, self.BOT_ALEXANDRA_GROUP_ID),
            (self.BOT_ANDREI_TOKEN, self.BOT_ANDREI_GROUP_ID),
        ):
            if token and group_id > 0:
                configured_group_ids.append(group_id)
        if len(configured_group_ids) != len(set(configured_group_ids)):
            raise ValueError(
                "Duplicate push-bot group_id: each configured push bot "
                "(BOT_IVAN/BOT_ALEXANDRA/BOT_ANDREI) must map to a distinct "
                "group_id (ADR-0027 §2)"
            )
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
    def admin_telegram_ids(self) -> list[int]:
        """Parsed ``ADMIN_TELEGRAM_IDS`` CSV (ADR-0027 §2).

        Splits on commas, trims whitespace and drops any element that is
        not a (optionally negative) integer — empty / malformed entries are
        silently discarded so an operator typo never aborts the worker.
        """
        ids: list[int] = []
        for raw in self.ADMIN_TELEGRAM_IDS.split(","):
            token = raw.strip()
            if token.lstrip("-").isdigit():
                ids.append(int(token))
        return ids

    @property
    def push_team_bots(self) -> list[PushTeamBot]:
        """Configured push-only per-team bots (ADR-0027 §2).

        A bot is included only when its token is non-empty AND its
        ``group_id`` is positive. If ``admin_telegram_ids`` is empty an empty
        list is returned — a bot with no recipients is meaningless, so the
        whole channel stays off.

        round-42 (ADR-0027 §2): each bot also carries its ``webhook_secret``.
        A bot stays in the list even with an empty ``webhook_secret`` — it is
        still needed for delivery; the callback button (and the push-webhook
        route, §10) only activate when the secret is non-empty.
        """
        if not self.admin_telegram_ids:
            return []
        bots: list[PushTeamBot] = []
        for name, token, group_id, webhook_secret in (
            ("ivan", self.BOT_IVAN_TOKEN, self.BOT_IVAN_GROUP_ID, self.BOT_IVAN_WEBHOOK_SECRET),
            (
                "alexandra",
                self.BOT_ALEXANDRA_TOKEN,
                self.BOT_ALEXANDRA_GROUP_ID,
                self.BOT_ALEXANDRA_WEBHOOK_SECRET,
            ),
            (
                "andrei",
                self.BOT_ANDREI_TOKEN,
                self.BOT_ANDREI_GROUP_ID,
                self.BOT_ANDREI_WEBHOOK_SECRET,
            ),
        ):
            if token and group_id > 0:
                bots.append(
                    PushTeamBot(
                        name=name,
                        token=token,
                        group_id=group_id,
                        webhook_secret=webhook_secret,
                    )
                )
        return bots

    @property
    def push_team_bots_enabled(self) -> bool:
        """True when at least one push-only per-team bot is configured AND
        there is at least one admin recipient (ADR-0027 §2)."""
        return bool(self.push_team_bots)

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
