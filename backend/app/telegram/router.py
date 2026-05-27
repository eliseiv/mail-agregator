"""Telegram webhook receiver + Persistent SSO endpoint
(ADR-0018 + ADR-0022; ``docs/04-api-contracts.md`` §4a).

Endpoints:

- ``POST /api/telegram/webhook/{secret}`` — Bot API updates (launcher).
- ``POST /api/telegram/auth``             — Persistent SSO (initData HMAC).

Authn for the webhook: dual-channel proof-of-Telegram —

1. ``{secret}`` URL-segment must equal ``settings.TELEGRAM_WEBHOOK_SECRET``
   (compared via :func:`secrets.compare_digest` to dodge timing oracles).
2. ``X-Telegram-Bot-Api-Secret-Token`` header, when present, must equal the
   same secret. Telegram sends this header iff ``setWebhook`` was called
   with ``secret_token=…``; we treat its presence as authoritative — when
   set, mismatch is fatal. Absence is tolerated only at the URL-secret
   level (some test fixtures invoke the endpoint without the header; ADR
   text says we accept that as long as the URL matches and the header,
   if present, also matches).

Authn for SSO: HMAC of ``init_data`` against the bot token + auth_date TTL.
No session, no CSRF — see :mod:`backend.app.telegram.init_data` and
:mod:`backend.app.telegram.sso_service`.

Why 404 (not 403) on secret mismatch: returning 404 keeps the webhook
endpoint unenumerable — an attacker probing random paths cannot
distinguish "wrong secret" from "wrong path", which is friendlier to
scanning hygiene (``docs/06-security.md`` §1.8 STRIDE-S). The contract
table at ``docs/04-api-contracts.md`` §4a still calls it ``403 forbidden``;
that behaviour is honoured by `NotFoundError` → ``not_found`` envelope
which nginx access logs as a 404 and Telegram retries the same way as
for 403.

These routes are exempt from CSRF (see ``backend/app/csrf.py``) and from
session resolution (the SessionMiddleware tolerates absence of
``mas_session`` — no extra exemption needed). Rate-limits are enforced
imperatively via :mod:`backend.app.rate_limit`.
"""

from __future__ import annotations

import secrets

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError as PydanticValidationError

from backend.app.cookies import set_session_cookies
from backend.app.deps import CurrentUser, DbSession
from backend.app.exceptions import (
    NotFoundError,
    RateLimitedError,
    ValidationError,
)
from backend.app.rate_limit import (
    LIMIT_TG_AUTH_IP,
    LIMIT_TG_AUTH_USER,
    LIMIT_TG_LINKS_WRITE,
    Limit,
    client_ip,
    consume,
)
from backend.app.repositories.telegram_links import TelegramLinksRepo
from backend.app.repositories.users import UsersRepo
from backend.app.sessions import SessionStore
from backend.app.telegram.bot import handle_update
from backend.app.telegram.callback_handler import handle_callback_query
from backend.app.telegram.schemas import (
    TelegramAuthRequest,
    TelegramAuthResponse,
    TelegramLinkAddRequest,
    TelegramLinkItem,
    TelegramLinksResponse,
    TelegramUpdate,
)
from backend.app.telegram.sso_service import (
    InvalidInitDataError,
    TelegramSSOService,
)
from shared.config import get_settings
from shared.logging import get_logger

log = get_logger(__name__)
router = APIRouter()


# Per ``docs/04-api-contracts.md`` §4a: 60 req/min per IP, defending against
# spoofed-update floods. Real Telegram traffic is dozens/day so this is well
# above legitimate volume.
_LIMIT_TG_WEBHOOK: Limit = Limit(name="tg_webhook", capacity=60, window_seconds=60)


def _secret_matches(provided: str, expected: str) -> bool:
    """Constant-time equality check tolerant of the ``expected==""`` case.

    The secret is an opaque hex string; we still avoid early-out on length
    by going through :func:`secrets.compare_digest`.
    """
    if not expected:
        return False
    return secrets.compare_digest(provided, expected)


@router.post("/api/telegram/webhook/{secret}")
async def telegram_webhook(secret: str, request: Request, db: DbSession) -> Response:
    """Telegram Bot API webhook endpoint.

    Returns 200 on every accepted request — even if the body is malformed
    or the bot is disabled — so Telegram drops the update from its retry
    queue. Only secret mismatch escapes as a 4xx.
    """
    settings = get_settings()

    # Rate-limit FIRST so secret-fail attempts also count against the cap
    # (else a probing attacker incurs no cost on each failed guess).
    try:
        await consume(_LIMIT_TG_WEBHOOK, f"ip:{client_ip(request)}")
    except RateLimitedError:
        # Bubble — handler envelope returns 429 with Retry-After.
        raise

    # Bot disabled — accept-and-drop. Still verify secret so a misconfigured
    # bot does not turn into an open POST endpoint that anyone can spam.
    if not settings.telegram_bot_enabled:
        # Per ADR-0018 §6: when TELEGRAM_BOT_ENABLED is false (or any
        # required env is empty), the route exists but is silent.
        return Response(status_code=200)

    expected = settings.TELEGRAM_WEBHOOK_SECRET

    # URL-path secret check.
    if not _secret_matches(secret, expected):
        log.info("telegram_webhook_invalid_secret", source="path")
        raise NotFoundError()

    # Header secret check — only enforced if Telegram actually sent it.
    # Telegram populates this header when setWebhook was invoked with
    # ``secret_token=…``; absence is OK (some setups omit it), but a
    # *mismatched* header is treated as fatal.
    header_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if header_secret and not _secret_matches(header_secret, expected):
        log.info("telegram_webhook_invalid_secret", source="header")
        raise NotFoundError()

    # Body parse — Telegram occasionally sends payloads we don't model
    # (edited_message etc.); we ignore unknown top-level keys but malformed
    # JSON or missing ``update_id`` is a parse error → log + 200.
    try:
        body = await request.json()
    except ValueError:
        log.warning("telegram_webhook_invalid_json")
        return Response(status_code=200)

    try:
        update = TelegramUpdate.model_validate(body)
    except PydanticValidationError:
        # Don't log the full body — it can contain user-typed message text
        # which counts as PII. Log just the keys present at top level so
        # we can debug Bot-API forward-compat.
        top_keys = sorted(body.keys()) if isinstance(body, dict) else []
        log.warning("telegram_webhook_invalid_update", top_keys=top_keys)
        return Response(status_code=200)

    # Bug-fix #5: callback_query updates fire when a user taps a button
    # rendered with ``callback_data`` (the «Посмотреть сообщение» button
    # on push notifications). Handled separately because it needs DB
    # access (visibility check + message lookup) and writes a follow-up
    # ``sendMessage`` reply in-chat.
    if update.callback_query is not None:
        await handle_callback_query(update.callback_query, db)
        return Response(status_code=200)

    await handle_update(update)
    return Response(status_code=200)


# ---------------------------------------------------------------------------
# Persistent SSO (ADR-0022 §1)
# ---------------------------------------------------------------------------


def _invalid_init_data_response(code: str, message: str) -> JSONResponse:
    """Canonical 401 envelope for SSO failures."""
    return JSONResponse(
        status_code=status.HTTP_401_UNAUTHORIZED,
        content={"error": {"code": code, "message": message}},
    )


@router.post("/api/telegram/auth")
async def telegram_auth(request: Request, db: DbSession) -> Response:
    """Persistent SSO endpoint (ADR-0022 §1.2).

    See ``docs/04-api-contracts.md`` §4a for the contract. Behaviour:

    1. Per-IP rate-limit (cheap; runs before HMAC).
    2. Parse + HMAC-validate the ``init_data`` body.
    3. Per-``telegram_user_id`` rate-limit (post-HMAC; replay defence).
    4. Lookup the link:
       - active → create a session for the linked user, return ``linked=true``.
       - missing → create a pending Redis token + cookie, return
         ``linked=false`` so the frontend redirects to ``/login``.

    Errors:

    - 401 ``invalid_init_data`` — HMAC mismatch / parse failure.
    - 401 ``init_data_expired`` — auth_date older than TTL.
    - 429 ``rate_limited``    — either bucket exhausted.
    """
    settings = get_settings()
    ip = client_ip(request)
    ua = request.headers.get("user-agent", "")

    # Per-IP rate-limit BEFORE HMAC so a flood of HMAC-fails counts here too.
    await consume(LIMIT_TG_AUTH_IP, f"ip:{ip}")

    # Parse JSON body — manual parse so a malformed payload becomes our
    # canonical 400 ``validation_error``.
    try:
        body = await request.json()
    except ValueError as exc:
        raise ValidationError("Body is not valid JSON") from exc
    try:
        payload = TelegramAuthRequest.model_validate(body)
    except PydanticValidationError as exc:
        raise ValidationError("Invalid auth payload") from exc

    if not settings.telegram_bot_enabled:
        # Without a bot token configured we cannot validate HMAC. Treat the
        # call the same as an invalid HMAC — opaque to clients (don't leak
        # configuration state).
        log.info("telegram_auth_bot_disabled")
        return _invalid_init_data_response("invalid_init_data", "initData validation failed")

    svc = TelegramSSOService(db)
    try:
        resolved = await svc.verify_and_resolve(payload.init_data)
    except InvalidInitDataError as exc:
        if exc.reason == "expired":
            log.info("telegram_auth_expired", ip=ip)
            return _invalid_init_data_response("init_data_expired", "initData expired")
        log.info("telegram_auth_invalid", reason=exc.reason, ip=ip)
        return _invalid_init_data_response("invalid_init_data", "initData validation failed")

    # Per-tg_user_id rate-limit (post-HMAC).
    await consume(LIMIT_TG_AUTH_USER, f"tg:{resolved.telegram_user_id}")

    if resolved.kind == "linked":
        assert resolved.user_id is not None
        user = await UsersRepo(db).get_by_id(resolved.user_id)
        # ``verify_and_resolve`` + ``get_by_id`` issued SELECTs that auto-began
        # a read-only transaction. Close it before either ``db.begin()`` write
        # block below — otherwise ``db.begin()`` raises "A transaction is
        # already begun on this Session" (same pattern as the ``current_user``
        # dependency in backend/app/deps.py).
        await db.commit()
        if user is None:
            # Link points at a user that was deleted out-of-band. Drop the
            # stale link and treat as ``unlinked`` — caller will go through
            # the usual login flow.
            log.warning(
                "telegram_auth_link_user_gone",
                user_id=resolved.user_id,
                telegram_user_id=resolved.telegram_user_id,
            )
            async with db.begin():
                await svc.revoke_for_user(
                    user_id=resolved.user_id,
                    reason="link_user_missing",
                    ip=ip,
                    user_agent=ua,
                )
            # fall through to the unlinked branch
            resolved_user_id_was_present = True
        else:
            # Round-17: Telegram SSO is a successful authentication for the
            # linked user. Without this call ``last_login_at`` stays at its
            # previous value (or NULL) for users that only sign in via TG.
            async with db.begin():
                await UsersRepo(db).record_login_success(user.id)
            session_token, csrf = await SessionStore().create(
                user.id, user.role, user.group_id, ip, ua
            )
            response = JSONResponse(
                content=TelegramAuthResponse(linked=True, redirect="/").model_dump(),
                status_code=status.HTTP_200_OK,
            )
            set_session_cookies(response, session_token, csrf, settings)
            log.info(
                "telegram_auth_linked",
                user_id=user.id,
                telegram_user_id=resolved.telegram_user_id,
            )
            return response
    else:
        resolved_user_id_was_present = False

    # Unlinked → create pending token + cookie.
    token = await svc.create_pending(resolved.telegram_user_id)
    response = JSONResponse(
        content=TelegramAuthResponse(linked=False, redirect="/login").model_dump(),
        status_code=status.HTTP_200_OK,
    )
    # Local import to avoid a circular import at module load (cookies.py
    # doesn't depend on telegram, but we want to keep the helper close to
    # other cookie writes).
    from backend.app.cookies import set_tg_pending_cookie

    set_tg_pending_cookie(response, token, settings)
    log.info(
        "telegram_auth_unlinked_pending_set",
        telegram_user_id=resolved.telegram_user_id,
        had_stale_link=resolved_user_id_was_present,
    )
    return response


# ---------------------------------------------------------------------------
# Multi-link management (ADR-0024 §4 — docs/04-api-contracts.md §4b)
# ---------------------------------------------------------------------------
# All three are cookie-authenticated (``CurrentUser`` dep) and CSRF-protected
# by the global middleware (not in the exempt list — only ``/api/telegram/auth``
# and the webhook prefix are exempt). They let an already-logged-in user manage
# several Telegram links (personal / work) from settings.


@router.get("/api/telegram/links")
async def list_telegram_links(db: DbSession, user: CurrentUser) -> JSONResponse:
    """List the current user's Telegram links (ADR-0024 §4).

    Returns every link (live and dead) so the UI can show dead ones with a
    re-link hint; ``dead`` reflects ``dead_at IS NOT NULL``.
    """
    links = await TelegramLinksRepo(db).list_by_user_id(user.id)
    payload = TelegramLinksResponse(
        links=[
            TelegramLinkItem(
                telegram_user_id=link.telegram_user_id,
                created_at=link.created_at.isoformat(),
                dead=link.dead_at is not None,
            )
            for link in links
        ],
        max=get_settings().TG_MAX_LINKS_PER_USER,
    )
    return JSONResponse(content=payload.model_dump())


@router.post("/api/telegram/links")
async def add_telegram_link(request: Request, db: DbSession, user: CurrentUser) -> JSONResponse:
    """Link a fresh Telegram account to the active session (ADR-0024 §4).

    Validates the supplied ``init_data`` HMAC (same as ``/api/telegram/auth``)
    and binds the resolved ``telegram_user_id`` to ``user.id`` directly — no
    pending-cookie flow. Applies the soft limit ``TG_MAX_LINKS_PER_USER`` and
    refuses re-binding a TG owned by a different internal user.
    """
    ip = client_ip(request)
    ua = request.headers.get("user-agent", "")[:256] or None
    await consume(LIMIT_TG_LINKS_WRITE, f"user:{user.id}")

    try:
        body = await request.json()
    except ValueError as exc:
        raise ValidationError("Body is not valid JSON") from exc
    try:
        payload = TelegramLinkAddRequest.model_validate(body)
    except PydanticValidationError as exc:
        raise ValidationError("Invalid link payload") from exc

    settings = get_settings()
    if not settings.telegram_bot_enabled:
        # No bot token → cannot validate HMAC. Opaque 401 (don't leak config).
        log.info("telegram_link_add_bot_disabled", user_id=user.id)
        return _invalid_init_data_response("invalid_init_data", "initData validation failed")

    svc = TelegramSSOService(db)
    try:
        resolved = await svc.verify_and_resolve(payload.init_data)
    except InvalidInitDataError as exc:
        if exc.reason == "expired":
            return _invalid_init_data_response("init_data_expired", "initData expired")
        return _invalid_init_data_response("invalid_init_data", "initData validation failed")

    # ``verify_and_resolve`` issued a SELECT, which auto-began a read-only
    # transaction on the session. Close it before opening the write
    # transaction below — otherwise ``db.begin()`` raises
    # "A transaction is already begun on this Session" (same pattern as the
    # ``current_user`` dependency in backend/app/deps.py).
    await db.commit()

    # link_session_add raises TelegramLinkLimitError / TelegramLinkOwnedByOtherError
    # (both DomainError → handled by the global envelope as 409).
    async with db.begin():
        await svc.link_session_add(
            telegram_user_id=resolved.telegram_user_id,
            user_id=user.id,
            ip=ip,
            user_agent=ua,
        )
    log.info(
        "telegram_link_added_via_session",
        user_id=user.id,
        telegram_user_id=resolved.telegram_user_id,
    )
    return JSONResponse(content={"linked": True, "telegram_user_id": resolved.telegram_user_id})


@router.delete("/api/telegram/links/{telegram_user_id}")
async def delete_telegram_link(
    telegram_user_id: int, request: Request, db: DbSession, user: CurrentUser
) -> JSONResponse:
    """Unlink a specific TG owned by the current user (ADR-0024 §4).

    Idempotent: ``{"deleted": false}`` (still 200) when no such link exists or
    it belongs to another user — never leaks ownership via a 404.
    """
    ip = client_ip(request)
    ua = request.headers.get("user-agent", "")[:256] or None
    await consume(LIMIT_TG_LINKS_WRITE, f"user:{user.id}")

    async with db.begin():
        deleted = await TelegramSSOService(db).revoke_one(
            user_id=user.id,
            telegram_user_id=telegram_user_id,
            ip=ip,
            user_agent=ua,
        )
    return JSONResponse(content={"deleted": deleted})


@router.delete("/api/telegram/links/{telegram_user_id}/delete")
async def delete_telegram_link_form(
    telegram_user_id: int, request: Request, db: DbSession, user: CurrentUser
) -> JSONResponse:
    """Form-fallback sibling for ``DELETE`` (ADR-0015, docs §4b).

    The no-JS unlink button POSTs to this ``/delete`` path with a hidden
    ``_method=DELETE`` field; :class:`MethodOverrideMiddleware` (the path is
    whitelisted) rewrites the POST into a DELETE that this handler serves.
    Delegates to the canonical handler logic.
    """
    return await delete_telegram_link(
        telegram_user_id=telegram_user_id, request=request, db=db, user=user
    )
