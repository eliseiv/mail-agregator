"""External API router — the connector's only machine surface.

ADR-0044 §4 (phase A1): the tags routes (``/api/external/tags*``), the teams
routes (``/api/external/teams``) and the ``group_id`` filter of
``GET /messages`` / ``GET /mailboxes`` went away with tags and teams
(ADR-0043 §4).

(ADR-0029 pull; ``docs/04-api-contracts.md`` §4d.)

``GET /api/external/messages`` — a B2B partner incrementally pulls ALL system
messages with a keyset cursor over ``messages.id`` (ADR-0029).

``POST /api/external/mailboxes/{id}/send`` — the generic send (ADR-0048 §1,
``docs/04-api-contracts.md`` §4f-send): the CRM builds the reply (defaults +
threading headers) and the aggregator only puts it on the wire from mailbox
``{id}``. Gated like the rest of the WRITE section (``EXTERNAL_WRITE_ENABLED`` +
``LIMIT_EXTERNAL_WRITE``). Answers ``{smtp_message_id}`` — no ``sent_id``, and it
writes NO ``sent_messages`` row. ADR-0048 §3 (phase A2.2): the legacy
message-scoped ``POST /api/external/messages/{id}/reply`` (ADR-0035) it superseded
— together with its ``sent_messages`` writer and the ``EXTERNAL_REPLY_ENABLED``
gate — was removed once the CRM was confirmed on this send in production.

Auth flow (strict order, ADR-0029 §4):

1. ``consume(<limit>, ip)`` FIRST — anti-flood before any work with the key (a
   failed-auth flood is rate-limited too). 429 on exhaustion. Read and WRITE use
   SEPARATE budgets (``LIMIT_EXTERNAL_API`` vs ``LIMIT_EXTERNAL_WRITE``) so
   neither can evict the other.
2. extract the key: ``X-API-Key`` (priority) or ``Authorization: Bearer <key>``.
3. feature off (``EXTERNAL_API_KEY`` empty) → 401 ``not_authenticated`` —
   unenumerable, the config is not disclosed.
4. missing / wrong key → 401 ``not_authenticated`` (constant-time compare).
5. (write only) write off (``EXTERNAL_WRITE_ENABLED`` false) → 403 ``forbidden``.
6. validate the request payload (query for pull; body for write — parsed AFTER
   steps 1-5 so a malformed/invalid body cannot pre-empt 401/403/429).
7. delegate to the service.

No cookie session is involved (ADR-0044 §5 removed the CSRF/session middlewares
along with the UI — there is nothing left to be exempt from). The key is NEVER
logged (redacted: ``EXTERNAL_API_KEY`` / ``X-API-Key`` / ``Authorization``).
"""

from __future__ import annotations

import secrets
from typing import TypeVar

from fastapi import APIRouter, Query, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError

from backend.app.deps import DbSession
from backend.app.exceptions import ForbiddenError, NotAuthenticatedError, NotFoundError
from backend.app.external.schemas import (
    ExternalMailboxCreateRequest,
    ExternalMailboxDTO,
    ExternalMailboxesResponse,
    ExternalMailboxSyncResponse,
    ExternalMailboxTestRequest,
    ExternalMailboxTestResponse,
    ExternalMailboxUpdateRequest,
    ExternalMessagesResponse,
    ExternalMessagesResponseDesc,
    ExternalOAuthAuthorizeRequest,
    ExternalOAuthAuthorizeResponse,
    ExternalSendRequest,
    ExternalSendResponse,
)
from backend.app.external.service import ExternalMessagesService
from backend.app.external.write_service import ExternalMailboxService
from backend.app.oauth.crm_ingest import notify_crm_oauth_ingest
from backend.app.oauth.service import OAuthError, OutlookOAuthService
from backend.app.rate_limit import (
    LIMIT_EXTERNAL_API,
    LIMIT_EXTERNAL_WRITE,
    Limit,
    client_ip,
    consume,
)
from backend.app.send.service import SendService
from shared.config import Settings, get_settings
from shared.logging import get_logger

# Body-model typevar bound for the generic manual parser.
_BodyT = TypeVar("_BodyT", bound=BaseModel)

log = get_logger(__name__)
router = APIRouter(prefix="/api/external")

# ADR-0029 §1: hard query bounds.
_DEFAULT_LIMIT: int = 50
_MAX_LIMIT: int = 200


def _bearer(authorization: str | None) -> str | None:
    """Extract ``<token>`` from an ``Authorization: Bearer <token>`` header.

    Returns ``None`` for a missing/malformed/non-Bearer header. Case-insensitive
    on the ``Bearer`` scheme; an empty token yields ``None``.
    """
    if not authorization:
        return None
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    token = parts[1].strip()
    return token or None


def _api_key_matches(provided: str, expected: str) -> bool:
    """Constant-time key comparison (ADR-0029 §Security).

    An empty ``expected`` (feature off) always returns ``False`` without an
    early-out on length — same pattern as the Telegram webhook secret check.
    """
    if not expected:
        return False
    return secrets.compare_digest(provided, expected)


def _authenticate(request: Request, *, ip: str, settings: Settings) -> None:
    """Shared external-API auth: key extract + feature gate + constant-time compare.

    Steps 2-4 of the auth flow (ADR-0029 §4), shared by every route in this
    router — the pull GETs and the WRITE section alike.
    Raises :class:`NotAuthenticatedError`
    (opaque 401) when the feature is off OR the key is missing/wrong — the two
    are indistinguishable so the config is never disclosed. The rate-limit
    (step 1) is consumed by the caller BEFORE this, so a failed-auth flood is
    throttled too. Never logs the key.
    """
    # X-API-Key takes priority, else Authorization: Bearer.
    key = request.headers.get("X-API-Key") or _bearer(request.headers.get("Authorization"))

    # Feature off — opaque 401 (do NOT reveal that the feature is disabled).
    if not settings.external_api_enabled:
        log.info("external_unauthorized", client_ip=ip)
        raise NotAuthenticatedError()

    # Missing / wrong key — same opaque 401 (constant-time compare; a None key
    # short-circuits to 401 without a compare — ADR-0029 §4).
    if key is None or not _api_key_matches(key, settings.EXTERNAL_API_KEY):
        log.info("external_unauthorized", client_ip=ip)
        raise NotAuthenticatedError()


# ADR-0036: two co-existing modes on one endpoint, selected by ``order``.
# ``response_model=None`` because the two modes return DISTINCT envelopes
# (``ExternalMessagesResponse`` with ``next_since_id`` for ``asc`` /
# ``ExternalMessagesResponseDesc`` with ``next_before_id`` for ``desc``) — each
# cursor field must be present ONLY in its own mode (ADR-0036 §3). A shared
# ``response_model`` would filter/merge the fields; returning the concrete model
# lets FastAPI serialise exactly the fields of the chosen mode.
@router.get("/messages", response_model=None)
async def list_external_messages(
    request: Request,
    db: DbSession,
    # ``order``/``before_id`` are intentionally NOT bound-validated by FastAPI
    # ``Query`` (no ``Literal`` on ``order``, no ``ge`` on ``before_id``): the
    # mode co-existence + bounds are validated in a DETERMINISTIC order inside
    # the service (``_validate_mode``, ADR-0036 §5) so the returned ``field`` is
    # predictable when several constraints are violated at once. ``since_id`` /
    # ``limit`` keep their ADR-0029 FastAPI bounds unchanged.
    order: str = Query(default="asc"),
    since_id: int = Query(default=0, ge=0),
    before_id: int | None = Query(default=None),
    limit: int = Query(default=_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT),
    # ADR-0039 §3: optional server-side filter, REPEATABLE. FastAPI parses
    # ``?mail_account_id=1&mail_account_id=2`` into a list; a single value stays
    # BC. No per-element ``ge`` bound — a missing/foreign/non-canonical id simply
    # does not appear in the intersection (empty page, not 404 / not 400).
    # ADR-0044 §4 (phase A1): the ``group_id`` filter went away with teams.
    mail_account_id: list[int] | None = Query(default=None),
) -> ExternalMessagesResponse | ExternalMessagesResponseDesc:
    """Incrementally pull system messages (ADR-0029 forward / ADR-0036 backward).

    See module docstring. ``order=asc`` (default) is the ADR-0029 forward keyset
    (BC, byte-for-byte); ``order=desc`` is the ADR-0036 newest-first mode
    (latest N when ``before_id`` is absent, older page when present).
    """
    ip = client_ip(request)
    settings = get_settings()

    # 1. Rate-limit FIRST — before any key work (anti-bruteforce + DoS). 429.
    #    Capacity is operator-tunable at consume-time from
    #    ``settings.EXTERNAL_API_RATE_LIMIT_PER_MINUTE`` (same override pattern
    #    as ``EXTERNAL_WRITE_RATE_LIMIT_PER_MINUTE``); the static
    #    ``LIMIT_EXTERNAL_API`` only supplies the name + fixed 60 s window.
    #    Both modes share the SAME budget (ADR-0036 §6 — backward is read too).
    runtime_limit = Limit(
        name=LIMIT_EXTERNAL_API.name,
        capacity=settings.EXTERNAL_API_RATE_LIMIT_PER_MINUTE,
        window_seconds=LIMIT_EXTERNAL_API.window_seconds,
    )
    await consume(runtime_limit, f"ip:{ip}")

    # 2-4. Auth: key extract + feature gate + constant-time compare.
    _authenticate(request, ip=ip, settings=settings)

    # 5. Mode validation (deterministic, ADR-0036 §5) + 6. delegate — both live
    #    in the service (after auth), which returns the mode-appropriate envelope.
    result = await ExternalMessagesService(db).list_messages(
        order=order,
        since_id=since_id,
        before_id=before_id,
        limit=limit,
        mail_account_ids=mail_account_id,
    )

    log.info(
        "external_pull",
        client_ip=ip,
        order=order,
        since_id=since_id,
        before_id=before_id,
        limit=limit,
        mail_account_id=mail_account_id,
        returned=len(result.messages),
    )
    return result


@router.get("/mailboxes", response_model=ExternalMailboxesResponse)
async def list_external_mailboxes(
    request: Request,
    db: DbSession,
    # ADR-0039 §4: optional filter over the canonical set — ``is_active``
    # (None = all). ADR-0044 §4 (phase A1): the ``group_id`` filter is gone.
    is_active: bool | None = Query(default=None),
) -> ExternalMailboxesResponse:
    """List canonical mailboxes with status for the CRM (ADR-0037 §2 / ADR-0039 §4).

    Same auth flow as the pull GET (ADR-0029 §4): rate-limit FIRST (shared
    ``LIMIT_EXTERNAL_API`` read budget), then key extract + feature gate +
    constant-time compare. Read — no write-gate. Canonical-dedup (ADR-0029 §5)
    so the set matches the mailboxes whose messages ``GET /messages`` returns.
    CSRF-exempt via the ``/api/external/`` prefix.
    """
    ip = client_ip(request)
    settings = get_settings()

    # 1. Rate-limit FIRST — before any key work (same budget as the pull GET).
    runtime_limit = Limit(
        name=LIMIT_EXTERNAL_API.name,
        capacity=settings.EXTERNAL_API_RATE_LIMIT_PER_MINUTE,
        window_seconds=LIMIT_EXTERNAL_API.window_seconds,
    )
    await consume(runtime_limit, f"ip:{ip}")

    # 2-4. Auth: key extract + feature gate + constant-time compare (401 opaque).
    _authenticate(request, ip=ip, settings=settings)

    result = await ExternalMessagesService(db).list_mailboxes(is_active=is_active)
    log.info(
        "external_mailboxes",
        client_ip=ip,
        is_active=is_active,
        returned=len(result.mailboxes),
    )
    return result


# ---------------------------------------------------------------------------
# External WRITE section — mailboxes (ADR-0039; the global tags CRUD it also
# carried went away with tags, ADR-0044 §4 phase A1).
#
# Auth-flow (strict order, ADR-0029 §4):
#   1. consume(LIMIT_EXTERNAL_WRITE, ip) FIRST — a SEPARATE budget from read so
#      a write flood can't evict it (and a failed-auth flood is throttled too).
#   2-4. shared key auth (X-API-Key / Bearer, feature gate, constant-time).
#   5. write-gate: EXTERNAL_WRITE_ENABLED false → 403 forbidden (even with a
#      valid key). Read (GET /mailboxes, /messages) has NO write-gate.
#   6. body parsed MANUALLY (``_parse_json_body``) AFTER 1-5 so a malformed /
#      invalid body cannot pre-empt 401/403/429.
# Path ids are plain ``int`` (no ``ge``) so an id < 1 resolves to 404 in the
# service, not a pre-auth 400.
# All routes CSRF-exempt via the ``/api/external/`` prefix.
# ---------------------------------------------------------------------------


async def _authorize_write(request: Request, *, ip: str, settings: Settings) -> None:
    """Rate-limit → key/gate/compare → write-gate (steps 1-5 above)."""
    runtime_limit = Limit(
        name=LIMIT_EXTERNAL_WRITE.name,
        capacity=settings.EXTERNAL_WRITE_RATE_LIMIT_PER_MINUTE,
        window_seconds=LIMIT_EXTERNAL_WRITE.window_seconds,
    )
    await consume(runtime_limit, f"ip:{ip}")
    _authenticate(request, ip=ip, settings=settings)
    if not settings.EXTERNAL_WRITE_ENABLED:
        log.info("external_write_forbidden", client_ip=ip, path=request.url.path)
        raise ForbiddenError("External write is disabled")


async def _parse_json_body(request: Request, model: type[_BodyT]) -> _BodyT:
    """Parse + validate a JSON body AFTER rate-limit/auth/gate (ADR-0035 §3 order).

    Reading the raw body and parsing it HERE (rather than via an auto-injected
    FastAPI body parameter, which validates during dependency resolution BEFORE
    the handler runs) keeps a malformed/invalid body from pre-empting the
    401/403/429 that must fire first.

    A malformed JSON body and a schema violation both surface as
    ``400 validation_error`` via the app's :class:`RequestValidationError`
    handler — identical envelope to auto-validated routes.
    """
    raw = await request.body()
    try:
        return model.model_validate_json(raw)
    except PydanticValidationError as exc:
        raise RequestValidationError(exc.errors()) from exc


# --- Mailboxes: write ------------------------------------------------------


@router.post("/mailboxes/test", response_model=ExternalMailboxTestResponse)
async def external_mailbox_test(request: Request, db: DbSession) -> ExternalMailboxTestResponse:
    """Probe IMAP/SMTP connectivity without persistence (ADR-0039 §2)."""
    ip = client_ip(request)
    settings = get_settings()
    await _authorize_write(request, ip=ip, settings=settings)
    payload = await _parse_json_body(request, ExternalMailboxTestRequest)
    result = await ExternalMailboxService(db).test(payload)
    log.info("external_mailbox_test", client_ip=ip, imap_ok=result.imap_ok, smtp_ok=result.smtp_ok)
    return result


@router.post("/mailboxes", response_model=ExternalMailboxDTO, status_code=status.HTTP_201_CREATED)
async def external_mailbox_create(request: Request, db: DbSession) -> ExternalMailboxDTO:
    """Create a mailbox owned by ``crm-service`` (ADR-0039 §2)."""
    ip = client_ip(request)
    settings = get_settings()
    await _authorize_write(request, ip=ip, settings=settings)
    payload = await _parse_json_body(request, ExternalMailboxCreateRequest)
    async with db.begin():
        dto = await ExternalMailboxService(db).create(payload)
    log.info("external_mailbox_created", client_ip=ip, mailbox_id=dto.id)
    return dto


@router.patch("/mailboxes/{account_id}", response_model=ExternalMailboxDTO)
async def external_mailbox_update(
    request: Request, db: DbSession, account_id: int
) -> ExternalMailboxDTO:
    """Update a mailbox: creds / hosts / display_name / is_active (ADR-0039 §2)."""
    ip = client_ip(request)
    settings = get_settings()
    await _authorize_write(request, ip=ip, settings=settings)
    payload = await _parse_json_body(request, ExternalMailboxUpdateRequest)
    service = ExternalMailboxService(db)
    async with db.begin():
        dto = await service.update(account_id, payload)
    # ADR-0046 §2 (H5/H6): mailbox-status hook fires AFTER the COMMIT, outside
    # the transaction — the dispatcher pushes the live DB snapshot, so an
    # enqueue from inside ``db.begin()`` could mirror the pre-commit state. For
    # a deactivation (``is_active=false``) that would stick FOREVER: the mailbox
    # leaves ``list_active()`` and never produces another status event.
    await service.flush_crm_status_events()
    log.info("external_mailbox_updated", client_ip=ip, mailbox_id=account_id)
    return dto


@router.delete("/mailboxes/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
async def external_mailbox_delete(request: Request, db: DbSession, account_id: int) -> Response:
    """Delete a mailbox (ADR-0039 §2). Its messages go away via the FK CASCADE."""
    ip = client_ip(request)
    settings = get_settings()
    await _authorize_write(request, ip=ip, settings=settings)
    async with db.begin():
        await ExternalMailboxService(db).delete(account_id)
    log.info("external_mailbox_deleted", client_ip=ip, mailbox_id=account_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/mailboxes/{account_id}/sync",
    response_model=ExternalMailboxSyncResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def external_mailbox_sync(
    request: Request, db: DbSession, account_id: int
) -> ExternalMailboxSyncResponse:
    """Force-sync a mailbox via the Redis ``force_sync:{id}`` marker (ADR-0039 §2)."""
    ip = client_ip(request)
    settings = get_settings()
    await _authorize_write(request, ip=ip, settings=settings)
    await ExternalMailboxService(db).sync(account_id)
    log.info("external_mailbox_sync_queued", client_ip=ip, mailbox_id=account_id)
    return ExternalMailboxSyncResponse(queued=True)


@router.post("/mailboxes/{account_id}/send", response_model=ExternalSendResponse)
async def external_mailbox_send(
    request: Request, db: DbSession, account_id: int
) -> ExternalSendResponse:
    """Generic SMTP send from mailbox ``{id}`` (ADR-0048 §1, phase A2.1).

    The endpoint the CRM calls to answer a message: the CRM owns the message
    store, the reply defaults and the threading headers; the aggregator only
    puts the MIME on the wire. Replaces the message-scoped reply above (removed
    in phase A2.2, ADR-0048 §3).

    Same auth flow / gate / budget as the rest of the external WRITE section
    (``_authorize_write``: ``LIMIT_EXTERNAL_WRITE`` rate-limit → ``X-API-Key`` /
    ``Bearer`` → ``EXTERNAL_WRITE_ENABLED`` → body). This matters more here than
    for the mailbox CRUD: a generic send can mail ANY recipient from ANY mailbox
    under the machine key (ADR-0048 §2 — the surface extension is deliberate and
    is compensated by exactly this gate + the CRM's own JWT/RBAC in front).

    ``account_id`` is a plain ``int`` path param (no ``ge``), so an id < 1
    resolves to ``404 not_found`` in the service rather than a pre-auth 400 —
    same precedent as the other write routes. Per ADR-0048 §4 a ``404`` here
    means **the MAILBOX is unknown** (not "no such message" — there is no message
    in this contract); the CRM maps it as a catalogue de-sync.

    Response is ``{smtp_message_id}`` only — no ``sent_id`` (ADR-0048 §1): the
    aggregator writes NO ``sent_messages`` row on this path, so it has no durable
    id to hand out; the CRM mints one from its own table.
    """
    ip = client_ip(request)
    settings = get_settings()
    await _authorize_write(request, ip=ip, settings=settings)
    payload = await _parse_json_body(request, ExternalSendRequest)

    # ``async with db.begin():`` even though nothing is persisted on this path:
    # the OAuth branch REFRESHES the Outlook access/refresh token inside the send
    # (``OutlookTokenService.get_valid_access_token`` → ``update_oauth_tokens``),
    # and ``get_db`` does not commit on teardown — without an explicit
    # transaction a rotated refresh-token would be rolled back at session close
    # and the next send would present a spent token. Domain errors (404/409/502)
    # propagate out of the block → rollback.
    async with db.begin():
        smtp_message_id = await SendService(db).send_from_mailbox(
            mail_account_id=account_id,
            to=payload.to,
            cc=payload.cc,
            subject=payload.subject,
            body_text=payload.body_text,
            in_reply_to=payload.in_reply_to,
            refs=payload.refs,
        )

    log.info(
        "external_send",
        client_ip=ip,
        mailbox_id=account_id,
        smtp_message_id=smtp_message_id,
    )
    return ExternalSendResponse(smtp_message_id=smtp_message_id)


# ---------------------------------------------------------------------------
# External Outlook OAuth (headless) — ADR-0045 / 04-api-contracts §4f-oauth.
#
# Restores the Outlook consent flow for adding/reconnecting mailboxes from the
# CRM: the session ``oauth/router.py`` was decommissioned with the cookie UI
# (ADR-0044 §5) and these routes are now the only consent entry point. The
# ``OUTLOOK_CLIENT_SECRET`` + code→token exchange + refresh-token AES-GCM stay
# in the aggregator; the CRM only initiates (opaque ``crm_state``) and receives
# the binding via a signed server-to-server notification (§3).
#
#   POST /api/external/mailboxes/oauth/authorize  — EXTERNAL_WRITE_ENABLED gated
#       (reuses ``_authorize_write``: rate-limit → key → gate → write-gate),
#       then the ``outlook_oauth_enabled`` 404-gate, then body → build URL.
#   GET  /api/external/mailboxes/oauth/callback   — the registered redirect_uri,
#       NO key/session (authorised by the one-shot Redis ``state`` + PKCE);
#       returns a self-contained HTML success/error page (no Jinja after the
#       demontage). CSRF-exempt via the ``/api/external/`` prefix.
# ---------------------------------------------------------------------------


def _require_outlook_oauth_enabled(settings: Settings) -> None:
    """Hide both OAuth routes (404) when the feature is off (ADR-0045 §2).

    Symmetric with the old session ``_require_enabled`` — the feature is hidden,
    not disclosed, when ``OUTLOOK_CLIENT_ID``/``_SECRET`` are unset.
    """
    if not settings.outlook_oauth_enabled:
        raise NotFoundError()


def _oauth_html_page(*, title: str, heading: str, message: str) -> HTMLResponse:
    """Minimal self-contained HTML page for the callback (no Jinja/templates).

    Inline strings only — the aggregator has no template engine after the
    demontage (ADR-0045 §2). ``Cache-Control: no-store`` since it may briefly
    reflect flow state. The static, non-user-controlled text is safe to inline
    (no request-derived interpolation → no XSS surface).
    """
    html = (
        '<!doctype html><html lang="ru"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{title}</title>"
        "<style>body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;"
        "background:#0f172a;color:#e2e8f0;display:flex;min-height:100vh;margin:0;"
        "align-items:center;justify-content:center}main{max-width:28rem;padding:2rem;"
        "text-align:center}h1{font-size:1.25rem;margin:0 0 .75rem}p{color:#94a3b8;"
        "line-height:1.5;margin:0}</style></head><body><main>"
        f"<h1>{heading}</h1><p>{message}</p></main></body></html>"
    )
    return HTMLResponse(content=html, headers={"Cache-Control": "no-store"})


# Static, non-user-controlled Russian page copy for the callback HTML.
_OAUTH_OK_TITLE = "Outlook подключён"
_OAUTH_OK_MESSAGE = "Ящик добавлен. Вернитесь в CRM — можно закрыть эту вкладку."
_OAUTH_ERR_TITLE = "Не удалось подключить Outlook"  # noqa: RUF001
_OAUTH_ERR_MESSAGE = (
    "Подключение не завершено. Вернитесь в CRM и повторите попытку добавления ящика."
)


def _oauth_success_page() -> HTMLResponse:
    return _oauth_html_page(
        title=_OAUTH_OK_TITLE, heading=_OAUTH_OK_TITLE, message=_OAUTH_OK_MESSAGE
    )


def _oauth_error_page() -> HTMLResponse:
    return _oauth_html_page(
        title=_OAUTH_ERR_TITLE, heading=_OAUTH_ERR_TITLE, message=_OAUTH_ERR_MESSAGE
    )


@router.post(
    "/mailboxes/oauth/authorize",
    response_model=ExternalOAuthAuthorizeResponse,
)
async def external_oauth_authorize(
    request: Request, db: DbSession
) -> ExternalOAuthAuthorizeResponse:
    """Mint a Microsoft authorize URL + state for a headless CRM consent (ADR-0045 §2).

    Order (ADR-0045 §2): ``_authorize_write`` (rate-limit → key → feature-gate →
    write-gate) → ``outlook_oauth_enabled`` 404-gate → body → delegate.
    """
    ip = client_ip(request)
    settings = get_settings()
    await _authorize_write(request, ip=ip, settings=settings)
    _require_outlook_oauth_enabled(settings)
    payload = await _parse_json_body(request, ExternalOAuthAuthorizeRequest)
    authorize_url, state = await OutlookOAuthService(db).build_authorize_url_headless(
        payload.crm_state
    )
    log.info("external_oauth_authorize", client_ip=ip)
    return ExternalOAuthAuthorizeResponse(authorize_url=authorize_url, state=state)


@router.get("/mailboxes/oauth/callback", response_model=None)
async def external_oauth_callback(
    request: Request,
    db: DbSession,
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    error: str | None = Query(default=None),
    error_description: str | None = Query(default=None),
) -> HTMLResponse:
    """Microsoft redirect target: exchange the code, create/relink, notify CRM (ADR-0045 §2).

    No API key/session — authorised by the one-shot Redis ``state`` (+ PKCE),
    consumed atomically inside ``exchange_code_headless``. Rate-limited by IP
    (the ``LIMIT_EXTERNAL_WRITE`` budget) since the redirect arrives without a
    key. Any failure (consent declined, missing/bad ``state``, exchange error)
    yields the HTML error page and creates NO mailbox.
    """
    ip = client_ip(request)
    settings = get_settings()

    # Rate-limit by IP — the redirect carries no key (ADR-0045 §2).
    runtime_limit = Limit(
        name=LIMIT_EXTERNAL_WRITE.name,
        capacity=settings.EXTERNAL_WRITE_RATE_LIMIT_PER_MINUTE,
        window_seconds=LIMIT_EXTERNAL_WRITE.window_seconds,
    )
    await consume(runtime_limit, f"ip:{ip}")

    # Feature hidden when Azure App creds are unset (404, symmetric with authorize).
    _require_outlook_oauth_enabled(settings)

    # Consent declined / Microsoft returned an error — no code to exchange.
    if error:
        log.info("external_oauth_consent_denied", client_ip=ip, error=error)
        return _oauth_error_page()

    if not code or not state:
        log.info("external_oauth_missing_params", client_ip=ip)
        return _oauth_error_page()

    try:
        async with db.begin():
            account, crm_state = await OutlookOAuthService(db).exchange_code_headless(
                code=code, state=state
            )
            # Capture the values BEFORE the transaction closes (expire-on-commit
            # would otherwise re-load these lazily outside a transaction).
            mail_account_id = int(account.id)
            email = account.email
            display_name = account.display_name
            is_active = bool(account.is_active)
        # ADR-0044 §4 (phase A3): the ``admin_audit`` write is removed BEFORE the
        # table drop (§3 lock-step) — the journal is not migrated to the CRM
        # (ADR-0043 §4); the linkage is still visible in the structured
        # ``external_oauth_connected`` log line below.
    except OAuthError as exc:
        log.info("external_oauth_exchange_failed", client_ip=ip, code=exc.code)
        return _oauth_error_page()

    # Best-effort CRM notification (§3) — a failure never rolls back the box.
    notified = await notify_crm_oauth_ingest(
        crm_state=crm_state,
        mail_account_id=mail_account_id,
        email=email,
        display_name=display_name,
        is_active=is_active,
    )
    log.info(
        "external_oauth_connected",
        client_ip=ip,
        mail_account_id=mail_account_id,
        crm_notified=notified,
    )
    return _oauth_success_page()
