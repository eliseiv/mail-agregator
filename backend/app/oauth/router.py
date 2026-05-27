"""HTTP routes for the Outlook OAuth2 flow (ADR-0025 §2, docs/04-api-contracts §4c).

- ``GET /api/oauth/outlook/authorize`` (session cookie): returns the Microsoft
  authorize URL as a JSON string (NOT a 302 — the user opens it in the right
  OctoBrowser profile).
- ``GET /api/oauth/outlook/callback`` (the registered redirect_uri): exchanges
  the code for tokens and creates/links the mail account, then 302s to
  ``/accounts``. CSRF-exempt (it is a GET; the Redis state is the anti-CSRF
  token, and the call may arrive without our session cookie — ADR-0025 Q-OAUTH-1).

Both routes return ``404 not_found`` when ``OUTLOOK_OAUTH_ENABLED`` is false
(feature hidden — symmetric with the telegram-bot-disabled behaviour).
"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request, status
from fastapi.responses import JSONResponse, RedirectResponse

from backend.app.audit import AuditWriter
from backend.app.deps import CurrentUser, DbSession
from backend.app.exceptions import DomainError, NotFoundError, ValidationError
from backend.app.oauth.schemas import OAuthAuthorizeResponse
from backend.app.oauth.service import OAuthError, OutlookOAuthService
from backend.app.rate_limit import (
    Limit,
    client_ip,
    consume,
)
from shared.config import get_settings
from shared.logging import get_logger

log = get_logger(__name__)

api = APIRouter(prefix="/api/oauth/outlook", tags=["oauth-outlook"])

# Rate-limits per docs/04-api-contracts.md §8: authorize 10/h per user;
# callback 30/min per IP.
LIMIT_OAUTH_AUTHORIZE = Limit(name="oauth_authorize", capacity=10, window_seconds=60 * 60)
LIMIT_OAUTH_CALLBACK = Limit(name="oauth_callback", capacity=30, window_seconds=60)


class OAuthFlowError(DomainError):
    """Maps an :class:`OAuthError.code` to the documented 400 wire codes
    (``oauth_state_invalid`` / ``oauth_exchange_failed`` /
    ``oauth_consent_denied``) — ADR-0025 §4c."""

    status_code = 400
    code = "oauth_flow_error"

    def __init__(self, code: str, message: str | None = None) -> None:
        super().__init__(message)
        self.code = code


def _require_enabled() -> None:
    """Hide the routes (404) when the feature is disabled (ADR-0025 §6)."""
    if not get_settings().outlook_oauth_enabled:
        raise NotFoundError()


@api.get("/authorize", response_model=OAuthAuthorizeResponse)
async def authorize(
    db: DbSession,
    user: CurrentUser,
) -> OAuthAuthorizeResponse:
    """Generate a Microsoft authorize URL + state for the current user."""
    _require_enabled()
    await consume(LIMIT_OAUTH_AUTHORIZE, str(user.id))
    authorize_url, state = await OutlookOAuthService(db).build_authorize_url(user.id)
    return OAuthAuthorizeResponse(authorize_url=authorize_url, state=state)


@api.get("/callback", response_model=None)
async def callback(
    request: Request,
    db: DbSession,
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    error: str | None = Query(default=None),
    error_description: str | None = Query(default=None),
) -> JSONResponse | RedirectResponse:
    """OAuth redirect target: exchange the code, link the account, redirect."""
    _require_enabled()
    await consume(LIMIT_OAUTH_CALLBACK, client_ip(request))

    # Microsoft sent ``error`` instead of ``code`` -> user declined / failed.
    if error:
        log.info("oauth_consent_denied", error=error)
        raise OAuthFlowError("oauth_consent_denied", "Consent was not granted")

    if not code or not state:
        raise ValidationError("Missing code or state", field="code")

    try:
        async with db.begin():
            service = OutlookOAuthService(db)
            account = await service.exchange_code(code=code, state=state)
            await AuditWriter(db).log(
                actor_user_id=account.user_id,
                action="oauth_account_linked",
                target_user_id=account.user_id,
                details={
                    "mail_account_id": account.id,
                    "email": account.email,
                    "scopes": account.oauth_scopes,
                },
                ip=client_ip(request),
                user_agent=request.headers.get("user-agent", "")[:256] or None,
            )
    except OAuthError as exc:
        raise OAuthFlowError(exc.code, exc.message) from exc

    return RedirectResponse(url="/accounts?outlook=connected", status_code=status.HTTP_302_FOUND)


router = APIRouter()
router.include_router(api)
