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
from typing import TypeVar

from fastapi import APIRouter, Query, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError

from backend.app.audit import AuditWriter
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
    ExternalReplyRequest,
    ExternalReplyResponse,
    ExternalTagApplyResponse,
    ExternalTagCreateRequest,
    ExternalTagFullDTO,
    ExternalTagRuleCreateRequest,
    ExternalTagRuleDTO,
    ExternalTagsResponse,
    ExternalTagUpdateRequest,
    ExternalTeamsResponse,
)
from backend.app.external.service import ExternalMessagesService
from backend.app.external.write_service import (
    ExternalMailboxService,
    ExternalTagsService,
)
from backend.app.oauth.crm_ingest import notify_crm_oauth_ingest
from backend.app.oauth.service import OAuthError, OutlookOAuthService
from backend.app.rate_limit import (
    LIMIT_EXTERNAL_API,
    LIMIT_EXTERNAL_REPLY,
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
    # ADR-0039 §3: optional server-side filters, now REPEATABLE and
    # AND-combinable (the ADR-0037 mutual-exclusion is superseded). FastAPI
    # parses ``?group_id=1&group_id=2`` into a list; a single value stays BC.
    # No per-element ``ge`` bound — a missing/foreign/non-canonical id simply
    # does not appear in the intersection (empty page, not 404 / not 400). The
    # intersection semantics live in the service (after auth, before any DB
    # call beyond the canonical resolve).
    mail_account_id: list[int] | None = Query(default=None),
    group_id: list[int] | None = Query(default=None),
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
        mail_account_ids=mail_account_id,
        group_ids=group_id,
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
    # ADR-0039 §4: optional filters over the canonical set. ``is_active`` (None =
    # all) and a REPEATABLE ``group_id`` (union). No per-element ``ge`` — a
    # foreign id simply narrows to nothing for that team.
    is_active: bool | None = Query(default=None),
    group_id: list[int] | None = Query(default=None),
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

    result = await ExternalMessagesService(db).list_mailboxes(
        is_active=is_active, group_ids=group_id
    )
    log.info(
        "external_mailboxes",
        client_ip=ip,
        is_active=is_active,
        group_id=group_id,
        returned=len(result.mailboxes),
    )
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


# ---------------------------------------------------------------------------
# External WRITE section — mailboxes + global tags CRUD (ADR-0039 / ADR-0040).
#
# Auth-flow (strict order, ADR-0029 §4 / ADR-0035 §3):
#   1. consume(LIMIT_EXTERNAL_WRITE, ip) FIRST — a SEPARATE budget from read /
#      reply so a write flood can't evict them (and a failed-auth flood is
#      throttled too).
#   2-4. shared key auth (X-API-Key / Bearer, feature gate, constant-time).
#   5. write-gate: EXTERNAL_WRITE_ENABLED false → 403 forbidden (even with a
#      valid key). Read (GET /tags, /mailboxes, /messages) has NO write-gate.
#   6. body parsed MANUALLY (``_parse_json_body``) AFTER 1-5 so a malformed /
#      invalid body cannot pre-empt 401/403/429.
# Path ids are plain ``int`` (no ``ge``) so an id < 1 resolves to 404 in the
# service, not a pre-auth 400 (same precedent as the reply endpoint).
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
    """Parse + validate a JSON body AFTER auth/gate (mirrors ``_parse_reply_body``).

    A malformed JSON body and a schema violation both surface as
    ``400 validation_error`` via the app's :class:`RequestValidationError`
    handler — identical envelope to auto-validated routes.
    """
    raw = await request.body()
    try:
        return model.model_validate_json(raw)
    except PydanticValidationError as exc:
        raise RequestValidationError(exc.errors()) from exc


# --- Tags: read (under EXTERNAL_API_KEY, no write-gate) ---------------------


@router.get("/tags", response_model=ExternalTagsResponse)
async def list_external_tags(request: Request, db: DbSession) -> ExternalTagsResponse:
    """List the global tag catalogue (ADR-0040 §4). Read — no write-gate."""
    ip = client_ip(request)
    settings = get_settings()
    runtime_limit = Limit(
        name=LIMIT_EXTERNAL_API.name,
        capacity=settings.EXTERNAL_API_RATE_LIMIT_PER_MINUTE,
        window_seconds=LIMIT_EXTERNAL_API.window_seconds,
    )
    await consume(runtime_limit, f"ip:{ip}")
    _authenticate(request, ip=ip, settings=settings)
    result = await ExternalTagsService(db).list()
    log.info("external_tags_list", client_ip=ip, returned=len(result.tags))
    return result


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
    """Update a mailbox: creds / hosts / display_name / group / is_active (ADR-0039 §2)."""
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
    """Delete a mailbox (+attachment/MinIO cascade) (ADR-0039 §2)."""
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


# --- Tags: write (global catalogue) ----------------------------------------


@router.post("/tags", response_model=ExternalTagFullDTO, status_code=status.HTTP_201_CREATED)
async def external_tag_create(request: Request, db: DbSession) -> ExternalTagFullDTO:
    """Create a global tag (ADR-0040 §4). ``409`` on name clash."""
    ip = client_ip(request)
    settings = get_settings()
    await _authorize_write(request, ip=ip, settings=settings)
    payload = await _parse_json_body(request, ExternalTagCreateRequest)
    async with db.begin():
        dto = await ExternalTagsService(db).create(payload)
    log.info("external_tag_created", client_ip=ip, tag_id=dto.id)
    return dto


@router.patch("/tags/{tag_id}", response_model=ExternalTagFullDTO)
async def external_tag_update(request: Request, db: DbSession, tag_id: int) -> ExternalTagFullDTO:
    """Update a global tag's name / color / match_mode (ADR-0040 §4)."""
    ip = client_ip(request)
    settings = get_settings()
    await _authorize_write(request, ip=ip, settings=settings)
    payload = await _parse_json_body(request, ExternalTagUpdateRequest)
    async with db.begin():
        dto = await ExternalTagsService(db).update(tag_id, payload)
    log.info("external_tag_updated", client_ip=ip, tag_id=tag_id)
    return dto


@router.delete("/tags/{tag_id}", status_code=status.HTTP_204_NO_CONTENT)
async def external_tag_delete(request: Request, db: DbSession, tag_id: int) -> Response:
    """Delete a global tag (ADR-0040 §4). Builtin → ``409 conflict``."""
    ip = client_ip(request)
    settings = get_settings()
    await _authorize_write(request, ip=ip, settings=settings)
    async with db.begin():
        await ExternalTagsService(db).delete(tag_id)
    log.info("external_tag_deleted", client_ip=ip, tag_id=tag_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/tags/{tag_id}/rules",
    response_model=ExternalTagRuleDTO,
    status_code=status.HTTP_201_CREATED,
)
async def external_tag_add_rule(request: Request, db: DbSession, tag_id: int) -> ExternalTagRuleDTO:
    """Add a rule to a global tag (ADR-0040 §4)."""
    ip = client_ip(request)
    settings = get_settings()
    await _authorize_write(request, ip=ip, settings=settings)
    payload = await _parse_json_body(request, ExternalTagRuleCreateRequest)
    async with db.begin():
        dto = await ExternalTagsService(db).add_rule(tag_id, payload)
    log.info("external_tag_rule_added", client_ip=ip, tag_id=tag_id, rule_id=dto.id)
    return dto


@router.delete("/tags/{tag_id}/rules/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def external_tag_delete_rule(
    request: Request, db: DbSession, tag_id: int, rule_id: int
) -> Response:
    """Delete a rule of a global tag (ADR-0040 §4)."""
    ip = client_ip(request)
    settings = get_settings()
    await _authorize_write(request, ip=ip, settings=settings)
    async with db.begin():
        await ExternalTagsService(db).delete_rule(tag_id, rule_id)
    log.info("external_tag_rule_deleted", client_ip=ip, tag_id=tag_id, rule_id=rule_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/tags/{tag_id}/apply-to-existing", response_model=ExternalTagApplyResponse)
async def external_tag_apply_to_existing(
    request: Request, db: DbSession, tag_id: int
) -> ExternalTagApplyResponse:
    """Apply a global tag's rules to all existing messages (ADR-0040 §4).

    ``422 tag_apply_too_many`` when the corpus exceeds ``APPLY_TO_EXISTING_LIMIT``.
    """
    ip = client_ip(request)
    settings = get_settings()
    await _authorize_write(request, ip=ip, settings=settings)
    async with db.begin():
        result = await ExternalTagsService(db).apply_to_existing(tag_id)
    log.info("external_tag_applied", client_ip=ip, tag_id=tag_id, applied=result.applied_count)
    return result


# ---------------------------------------------------------------------------
# External Outlook OAuth (headless) — ADR-0045 / 04-api-contracts §4f-oauth.
#
# Restores the Outlook consent flow for adding/reconnecting mailboxes from the
# CRM after the session ``oauth/router.py`` is decommissioned. The
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
            await AuditWriter(db).log(
                actor_user_id=account.user_id,
                action="oauth_account_linked",
                target_user_id=account.user_id,
                details={
                    "mail_account_id": mail_account_id,
                    "email": email,
                    "scopes": account.oauth_scopes,
                },
                ip=ip,
                user_agent=request.headers.get("user-agent", "")[:256] or None,
            )
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
