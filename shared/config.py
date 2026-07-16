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

    # --- Crypto (mail account passwords, AES-256-GCM, ADR-0005) ---
    MAIL_ENCRYPTION_KEY: str = ""  # base64 of exactly 32 raw bytes
    MAIL_ENCRYPTION_KEY_PREV: str | None = None  # only during rotation

    # --- Worker / sync ---
    MAX_CONCURRENT_IMAP: int = Field(default=10, ge=1, le=100)
    WORKER_THREAD_POOL_SIZE: int = Field(default=14, ge=1, le=200)
    SYNC_INTERVAL_MINUTES: int = Field(default=5, ge=1, le=60)
    RETENTION_DAYS: int = Field(default=30, ge=1, le=3650)
    IMAP_TIMEOUT_SECONDS: int = Field(default=60, ge=5, le=600)
    INITIAL_SYNC_DAYS: int = Field(default=30, ge=1, le=365)
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
    # on an otherwise-working mailbox must not be reported to the CRM as a
    # mailbox problem. If ``last_synced_at`` is older than this window (or NULL) the
    # transient error IS written (the sync is genuinely stuck). The
    # consecutive-failures counter is never touched either way.
    SYNC_TRANSIENT_SUPPRESS_MINUTES: int = Field(default=60, ge=0, le=10_080)
    # ADR-0028 kill-switch: when True (default), a sporadic Microsoft IMAP
    # "login failed" / "authenticationfailed" on an ``oauth_outlook`` account is
    # classified TRANSIENT (rule 7b) and retried instead of permanently
    # disabling the mailbox — the refresh already proved the token valid BEFORE
    # IMAP, so it is a server flake, not an auth failure. Set False to revert to
    # the pre-fix behaviour (OAuth "login failed" -> permanent instant-disable)
    # without a code redeploy. Password accounts are unaffected either way.
    SYNC_OAUTH_LOGIN_FAILED_TRANSIENT: bool = Field(default=True)

    # --- Mailbox connection-test hard-deadline (ADR-0047 §5) --------------
    # Upper bound of the PROBE part of a connection test (host-assert + IMAP
    # login + SMTP login, or the OAuth equivalent). Applied as
    # ``asyncio.wait_for`` INSIDE ``MailAccountService._test_credentials`` /
    # ``._test_oauth_account``, so ``POST /mailboxes/test``, ``POST /mailboxes``
    # and ``PATCH /mailboxes/{id}`` (creds change) all inherit it. Exhaustion →
    # domain 422 (``imap_login_failed`` / ``smtp_login_failed``,
    # ``details.detail="timeout"``), never a hang and never a 504.
    #
    # ``le=45`` machine-guards the THREE-TERM invariant (ADR-0047 §2.1/§2.3/§5).
    # The deadline bounds the PROBE, not the response: ``asyncio.wait_for`` AWAITS
    # the cancelled task, including the ``finally: await _close_smtp_client(...)``
    # of the SMTP probe (``accounts/testers.py:210-211``, ``:355-356``) — a polite
    # QUIT time-boxed by ``_SMTP_QUIT_TIMEOUT = 5`` (``testers.py:65``, ``:163-168``).
    # Hence the response time has three terms, and all three are budgeted:
    #   deadline (≤45) + teardown after cancellation (≤5, ADR-0047 §2.3)
    #                  + non-probe part of the request (≤5: auth, one SELECT by
    #                    PK, AES-decrypt, INSERT/UPDATE, DTO serialisation)
    #   = ≤55 < 60 = nginx ``proxy_read_timeout`` = gunicorn ``--timeout``
    # i.e. the domain 422 JSON always reaches the client BEFORE our own proxy
    # would turn the wait into a 504 HTML page. ``le`` formula (recompute on any
    # change of a term): ``le <= 60 - teardown(5) - non-probe(5) - reserve(5) = 45``.
    # ``le=50`` (the previous bound) and ``le=55`` are both over-claims: at 50 the
    # worst case is exactly ``50 + 5 + 5 = 60`` — the very 504 HTML this ADR exists
    # to prevent. env rather than a module constant because the correct value
    # follows ``proxy_read_timeout``, which devops retunes without a code release.
    # The fail-fast constants ``_IMAP_TIMEOUT``/``_SMTP_TIMEOUT``
    # (``accounts/testers.py``) stay module constants — they are subordinate to
    # this deadline, not the other way round.
    MAILBOX_TEST_DEADLINE_SECONDS: int = Field(default=45, ge=10, le=45)

    # --- External PULL-API (ADR-0029) -------------------------------------
    # Static API key authenticating ``GET /api/external/messages`` (the B2B
    # partner incrementally pulls ALL system messages). OPTIONAL: empty means
    # the feature is OFF — the endpoint then returns 401 unenumerably (it must
    # NOT be added to ``_enforce_required``). Generate with
    # ``openssl rand -hex 32`` (256-bit). Compared via constant-time
    # ``secrets.compare_digest``; redacted in ``shared/logging.py`` alongside
    # ``X-API-Key`` / ``Authorization``. Passed only to the ``api`` container.
    EXTERNAL_API_KEY: str = ""
    # Operator-tunable rate limit for ``GET /api/external/messages`` (ADR-0029
    # §1/§4): requests per minute, per client IP. Overrides the static
    # ``LIMIT_EXTERNAL_API`` capacity at consume-time (same pattern as
    # ``EXTERNAL_WRITE_RATE_LIMIT_PER_MINUTE``) so the cap can be retuned
    # without a code redeploy. Numeric (requests/60s), NOT a
    # ``"120/minute"`` string. Window is fixed at 60 s.
    EXTERNAL_API_RATE_LIMIT_PER_MINUTE: int = Field(default=120, ge=1, le=10000)
    # --- External write API — mailboxes CRUD (ADR-0039 / ADR-0040) --------
    # Separate write-gate for the headless-CRM write section
    # (``POST/PATCH/DELETE /api/external/mailboxes*``). ADR-0044 §4 (phase A1)
    # removed the tags routes (``/api/external/tags*``) — mailboxes is all that
    # remains behind this gate.
    # The read API (ADR-0029) is gated by ``EXTERNAL_API_KEY`` alone; every
    # mailboxes write additionally requires ``EXTERNAL_WRITE_ENABLED=true``.
    # Default ``false`` keeps a
    # read-only ADR-0029 deployment read-only after a code upgrade — a valid key
    # does NOT silently gain write capability. When false the write endpoints
    # return ``403 forbidden`` even for a valid key. Passed only to the ``api``
    # container. See ADR-0039 §1.
    EXTERNAL_WRITE_ENABLED: bool = False
    # Operator-tunable rate limit for the external write section (ADR-0039 §1):
    # requests per minute, per client IP. A SEPARATE budget from read (120) —
    # write is more expensive / abuse-sensitive, so it must not
    # share (no mutual eviction between pull and write). Overrides the
    # static ``LIMIT_EXTERNAL_WRITE`` capacity at consume-time (same pattern as
    # ``EXTERNAL_API_RATE_LIMIT_PER_MINUTE``). Numeric (requests/60s); window is
    # fixed at 60 s.
    EXTERNAL_WRITE_RATE_LIMIT_PER_MINUTE: int = Field(default=60, ge=1, le=10000)

    # --- Outlook OAuth2 (ADR-0025, Sprint B) ------------------------------
    # Azure App (Personal Microsoft accounts only) credentials. When BOTH
    # client id and secret are set, ``outlook_oauth_enabled`` flips on and the
    # ``/api/external/mailboxes/oauth/*`` routes (ADR-0045 §4 — the session
    # ``/api/oauth/outlook/*`` routes went away with the cookie UI) serve real
    # flows; otherwise they 404 (route hidden).
    OUTLOOK_CLIENT_ID: str = ""
    # Marked redact in ``shared/logging.py`` (alongside MAIL_ENCRYPTION_KEY).
    OUTLOOK_CLIENT_SECRET: str = ""
    # Registered Azure redirect_uri — must match Azure exactly. ADR-0045 §4
    # (amends ADR-0044 Phase G): the headless external-OAuth callback replaces
    # the removed session callback, so this is now
    # ``{APP_BASE_URL}/api/external/mailboxes/oauth/callback`` (updated at
    # cut-over by devops — the code only reads the env value).
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

    # --- CRM push connector (ADR-0043 §2) ---------------------------------
    # The aggregator PUSHes every newly synced message to the CRM and mirrors
    # mailbox sync-status changes. All three CRM_* below are read by the WORKER
    # container. Secrets/URLs are never logged (``CRM_PUSH_SECRET`` is in the
    # ``shared/logging.py`` redact-list).
    #
    # Ingest endpoint (new mail): ``POST {CRM_INGEST_URL}/api/mail/ingest``.
    CRM_INGEST_URL: str = ""
    # Mailbox status-channel: ``POST {CRM_MAILBOX_STATUS_URL}/api/mail/mailbox-status``.
    CRM_MAILBOX_STATUS_URL: str = ""
    # Shared HMAC-SHA256 secret (= CRM ``MAIL_PUSH_SECRET``). Empty on EITHER
    # side ⇒ the push channel stays OFF (jobs are not registered, sync_cycle
    # does not enqueue) so a pre-cut-over deployment keeps working unchanged.
    CRM_PUSH_SECRET: str = ""  # secret
    # Per-tick ``LPOP count`` from ``crm_push_queue`` = batch size POSTed to
    # ``/api/mail/ingest`` in one request. CRM caps the batch at 100
    # (ADR-044 §3 ``MAIL_INGEST_MAX_BATCH``); never exceed it.
    CRM_PUSH_BATCH_SIZE: int = Field(default=100, ge=1, le=100)
    # How often ``crm_push_dispatch`` drains ``crm_push_queue`` (~5s).
    CRM_PUSH_DISPATCH_INTERVAL_SECONDS: int = Field(default=5, ge=1, le=600)
    # ``crm_push_recovery`` cadence — re-enqueues messages with
    # ``pushed_at IS NULL`` (lost between sync and push, or a failed POST).
    CRM_PUSH_RECOVERY_INTERVAL_SECONDS: int = Field(default=3600, ge=60, le=86_400)
    # Lookback window for the recovery scan (bounded by the retention window,
    # ADR-0011 30 days). Rows older than this are past retention anyway.
    CRM_PUSH_RECOVERY_WINDOW_HOURS: int = Field(default=720, ge=1, le=8760)
    # Per-tick recovery re-enqueue batch cap.
    CRM_PUSH_RECOVERY_BATCH_SIZE: int = Field(default=500, ge=1, le=10_000)
    # How often ``crm_status_dispatch`` drains ``crm_status_queue`` (~5s).
    CRM_STATUS_DISPATCH_INTERVAL_SECONDS: int = Field(default=5, ge=1, le=600)
    # Per-tick ``LPOP count`` from ``crm_status_queue``.
    CRM_STATUS_BATCH_SIZE: int = Field(default=30, ge=1, le=500)
    # Total httpx timeout (connect+read+write) for the CRM POSTs.
    CRM_PUSH_HTTP_TIMEOUT_SECONDS: int = Field(default=10, ge=1, le=120)

    # --- External Outlook OAuth ingest (ADR-0045 §3) ----------------------
    # After a successful headless Outlook create/relink, the external callback
    # notifies the CRM of the mailbox↔team binding via
    # ``POST {CRM_OAUTH_INGEST_URL}`` (= CRM ``/api/mail/oauth/ingest``), signed
    # with the SAME HMAC scheme + reused ``CRM_PUSH_SECRET`` as
    # ``/api/mail/ingest`` (ADR-0043 §2). Empty ⇒ the notification is not sent
    # (headless-OAuth ingest effectively off — symmetric with
    # ``crm_status_enabled``). Read by the BACKEND container. Never logged.
    CRM_OAUTH_INGEST_URL: str = ""

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
    def external_api_enabled(self) -> bool:
        """True only when ``EXTERNAL_API_KEY`` is configured (ADR-0029 §4).

        Derived flag — symmetric with :pyattr:`outlook_oauth_enabled`. When false the
        ``GET /api/external/messages`` endpoint returns 401 unenumerably
        (feature off is indistinguishable from a wrong key — the config is
        never disclosed). Lets us register the route in dev/CI without a key.
        """
        return bool(self.EXTERNAL_API_KEY)

    @property
    def outlook_oauth_enabled(self) -> bool:
        """True only when both Azure App credentials are configured (ADR-0025 §6).

        Derived flag — symmetric with :pyattr:`external_api_enabled`. When
        false the ``/api/external/mailboxes/oauth/*`` routes return 404
        (feature hidden, ADR-0045 §4); lets us wire the routes in dev/CI
        without a real Azure App.
        """
        return bool(self.OUTLOOK_CLIENT_ID and self.OUTLOOK_CLIENT_SECRET)

    @property
    def crm_push_enabled(self) -> bool:
        """True when the CRM ingest push is fully configured (ADR-0043 §2).

        Requires ``CRM_INGEST_URL`` + ``CRM_PUSH_SECRET``. When false the
        ``crm_push_dispatch`` / ``crm_push_recovery`` jobs are NOT registered
        and ``sync_cycle`` does NOT enqueue to ``crm_push_queue`` — the
        pre-cut-over aggregator keeps working unchanged. Derived flag —
        symmetric with :pyattr:`external_api_enabled`.
        """
        return bool(self.CRM_INGEST_URL and self.CRM_PUSH_SECRET)

    @property
    def crm_status_enabled(self) -> bool:
        """True when the CRM mailbox-status channel is configured (ADR-0043 §2).

        Requires ``CRM_MAILBOX_STATUS_URL`` + ``CRM_PUSH_SECRET``. When false
        the ``crm_status_dispatch`` job is NOT registered and no status event is
        enqueued on a mailbox disable / re-enable transition.
        """
        return bool(self.CRM_MAILBOX_STATUS_URL and self.CRM_PUSH_SECRET)

    @property
    def crm_oauth_ingest_enabled(self) -> bool:
        """True when the headless-OAuth CRM ingest notification is configured (ADR-0045 §3).

        Requires ``CRM_OAUTH_INGEST_URL`` + ``CRM_PUSH_SECRET`` (reused). When
        false the external callback creates/relinks the mailbox but does NOT
        POST the binding to the CRM (best-effort, reconcile backfills — CRM
        TD-047). Derived flag — symmetric with :pyattr:`crm_status_enabled`.
        """
        return bool(self.CRM_OAUTH_INGEST_URL and self.CRM_PUSH_SECRET)

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
