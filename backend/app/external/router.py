"""External API router (ADR-0029 pull + ADR-0035 reply; ``docs/04-api-contracts.md`` §4d).

``GET /api/external/messages`` — a B2B partner incrementally pulls ALL system
messages with a keyset cursor over ``messages.id`` (ADR-0029).

``POST /api/external/messages/{id}/reply`` — the single WRITE endpoint (ADR-0035):
reply to an existing message with the same key. Narrow surface — no CRUD, no
arbitrary send, no ``from`` selection (sender = the original's mailbox).

Auth flow (strict order, ADR-0029 §4 / ADR-0035 §3):

1. ``consume(<limit>, ip)`` FIRST — anti-flood before any work with the key (a
   failed-auth flood is rate-limited too). 429 on exhaustion. The read and the
   reply endpoints use SEPARATE budgets (``LIMIT_EXTERNAL_API`` vs
   ``LIMIT_EXTERNAL_REPLY``) so neither can evict the other (ADR-0035 §4).
2. extract the key: ``X-API-Key`` (priority) or ``Authorization: Bearer <key>``.
3. feature off (``EXTERNAL_API_KEY`` empty) → 401 ``not_authenticated`` —
   unenumerable, the config is not disclosed.
4. missing / wrong key → 401 ``not_authenticated`` (constant-time compare).
5. (reply only) write off (``EXTERNAL_REPLY_ENABLED`` false) → 403 ``forbidden``.
6. validate the request payload (query for pull; body for reply — parsed AFTER
   steps 1-5 so a malformed/invalid body cannot pre-empt 401/403/429, ADR-0035
   §3 order + reviewer note).
7. delegate to the service.

Both routes are CSRF-exempt (``backend/app/csrf.py`` — the ``/api/external/``
prefix) and need no cookie session. The key is NEVER logged (redacted:
``EXTERNAL_API_KEY`` / ``X-API-Key`` / ``Authorization``).
"""

from __future__ import annotations

import secrets

from fastapi import APIRouter, Query, Request
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError as PydanticValidationError

from backend.app.deps import DbSession
from backend.app.exceptions import ForbiddenError, NotAuthenticatedError
from backend.app.external.schemas import (
    ExternalMailboxesResponse,
    ExternalMessagesResponse,
    ExternalMessagesResponseDesc,
    ExternalReplyRequest,
    ExternalReplyResponse,
    ExternalTeamsResponse,
)
from backend.app.external.service import ExternalMessagesService
from backend.app.rate_limit import (
    LIMIT_EXTERNAL_API,
    LIMIT_EXTERNAL_REPLY,
    Limit,
    client_ip,
    consume,
)
from backend.app.send.service import SendService
from shared.config import Settings, get_settings
from shared.logging import get_logger

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

    Steps 2-4 of the auth flow (ADR-0029 §4), reused by the pull GET and the
    reply POST (ADR-0035 §Migration step 4). Raises :class:`NotAuthenticatedError`
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
    # ADR-0037: optional server-side filters (narrow the canonical set). Bounds
    # (``ge=1``) are FastAPI-validated → ``400 field=mail_account_id``/``group_id``
    # for ``<1``/non-numeric. The mutual-exclusion (``field=filter``) and the
    # "missing/foreign id → empty page (not 404)" semantics live in the service
    # (after auth, before any DB call).
    mail_account_id: int | None = Query(default=None, ge=1),
    group_id: int | None = Query(default=None, ge=1),
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
    #    as ``WEBHOOK_TEST_LIMIT`` / ``TG_SEND_PER_CHAT_PER_MINUTE``); the static
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
        mail_account_id=mail_account_id,
        group_id=group_id,
    )

    log.info(
        "external_pull",
        client_ip=ip,
        order=order,
        since_id=since_id,
        before_id=before_id,
        limit=limit,
        mail_account_id=mail_account_id,
        group_id=group_id,
        returned=len(result.messages),
    )
    return result


@router.get("/teams", response_model=ExternalTeamsResponse)
async def list_external_teams(
    request: Request,
    db: DbSession,
) -> ExternalTeamsResponse:
    """List all system teams for the CRM (ADR-0037 §1).

    Same auth flow as the pull GET (ADR-0029 §4): rate-limit FIRST (shared
    ``LIMIT_EXTERNAL_API`` budget), then key extract + feature gate +
    constant-time compare. Read-only, super_admin-visibility, minimal
    ``id``/``name`` projection. CSRF-exempt via the ``/api/external/`` prefix.
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

    result = await ExternalMessagesService(db).list_teams()
    log.info("external_teams", client_ip=ip, returned=len(result.teams))
    return result


@router.get("/mailboxes", response_model=ExternalMailboxesResponse)
async def list_external_mailboxes(
    request: Request,
    db: DbSession,
) -> ExternalMailboxesResponse:
    """List all canonical mailboxes with status for the CRM (ADR-0037 §2).

    Same auth flow as the pull GET (ADR-0029 §4): rate-limit FIRST (shared
    ``LIMIT_EXTERNAL_API`` budget), then key extract + feature gate +
    constant-time compare. Canonical-dedup (ADR-0029 §5) so the set matches the
    mailboxes whose messages ``GET /messages`` returns. CSRF-exempt via the
    ``/api/external/`` prefix.
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

    result = await ExternalMessagesService(db).list_mailboxes()
    log.info("external_mailboxes", client_ip=ip, returned=len(result.mailboxes))
    return result


async def _parse_reply_body(request: Request) -> ExternalReplyRequest:
    """Read + validate the reply body AFTER rate-limit/auth/gate (ADR-0035 §3).

    FastAPI validates an auto-injected Pydantic body parameter during dependency
    resolution — BEFORE the handler runs — so a 400 could pre-empt 401/403/429
    and violate the ADR-0035 §3 order. We therefore read the raw body and parse
    it here, at the correct point in the sequence (reviewer note). A malformed
    JSON body or a schema violation both surface as ``400 validation_error``
    with ``details.errors[]`` via the app's :class:`RequestValidationError`
    handler (identical envelope to auto-validated routes).
    """
    raw = await request.body()
    try:
        # ``model_validate_json`` raises ``pydantic.ValidationError`` for BOTH a
        # syntactically invalid JSON body and a schema violation — one path.
        return ExternalReplyRequest.model_validate_json(raw)
    except PydanticValidationError as exc:
        raise RequestValidationError(exc.errors()) from exc


@router.post("/messages/{message_id}/reply", response_model=ExternalReplyResponse)
async def reply_external_message(
    request: Request,
    db: DbSession,
    message_id: int,
) -> ExternalReplyResponse:
    """Reply to an existing message (ADR-0035). See module docstring.

    ``message_id`` is a plain ``int`` path param: a non-numeric segment fails
    routing, and ``id < 1`` (no such message) resolves to ``404 not_found`` in
    the service — NOT a pre-auth 400 — keeping the ADR-0035 §3 check order
    (rate-limit → auth → gate → resolve) intact.
    """
    ip = client_ip(request)
    settings = get_settings()

    # 1. Rate-limit FIRST — SEPARATE, stricter budget than the read endpoint
    #    (ADR-0035 §4). Capacity is operator-tunable at consume-time from
    #    ``settings.EXTERNAL_REPLY_RATE_LIMIT_PER_MINUTE``.
    runtime_limit = Limit(
        name=LIMIT_EXTERNAL_REPLY.name,
        capacity=settings.EXTERNAL_REPLY_RATE_LIMIT_PER_MINUTE,
        window_seconds=LIMIT_EXTERNAL_REPLY.window_seconds,
    )
    await consume(runtime_limit, f"ip:{ip}")

    # 2-4. Auth (shared with the pull endpoint) — 401 opaque.
    _authenticate(request, ip=ip, settings=settings)

    # 5. Write gate — valid key but write disabled → 403 (ADR-0035 §1/§3).
    if not settings.EXTERNAL_REPLY_ENABLED:
        log.info("external_reply_forbidden", client_ip=ip, message_id=message_id)
        raise ForbiddenError("External reply is disabled")

    # 6. Body validation — AFTER rate-limit + auth + gate (ADR-0035 §3 order).
    payload = await _parse_reply_body(request)

    # 7. Delegate: canonical-scope resolve → from = original mailbox → send
    #    core (MIME/SMTP/append/persist reused). 404/409/502 propagate from
    #    the send core as domain errors.
    #
    #    ``get_db`` does NOT commit on teardown and ``SentMessagesRepo.insert``
    #    only add+flushes, so without an explicit transaction the persisted
    #    ``sent_messages`` row is rolled back at session close — the 200 would
    #    return a ``sent_id`` of a non-durable row (ADR-0035 §5/§7 violation).
    #    Mirror the session send (``backend/app/send/router.py``): wrap the
    #    send core in ``async with db.begin():`` so a successful send COMMITS
    #    the row (durable ``sent_id``) and any domain error (404/409/502) rolls
    #    back partial state as it propagates out. The best-effort IMAP append
    #    failure is swallowed inside ``_send_core`` (not raised), so an append
    #    error still commits ``sent_messages`` and yields 200. No form-fallback
    #    here, so domain errors simply propagate out of the block (→ rollback).
    async with db.begin():
        result = await SendService(db).send_external_reply(
            message_id=message_id,
            to=payload.to,
            cc=payload.cc,
            subject=payload.subject,
            body=payload.body,
        )

    log.info(
        "external_reply",
        client_ip=ip,
        message_id=message_id,
        sent_id=result.sent_id,
        smtp_message_id=result.smtp_message_id,
    )
    return ExternalReplyResponse(
        sent_id=result.sent_id,
        smtp_message_id=result.smtp_message_id,
    )
