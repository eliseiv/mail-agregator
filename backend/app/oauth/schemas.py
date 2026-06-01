"""Pydantic schemas + constants for the OAuth Outlook module (ADR-0025)."""

from __future__ import annotations

from typing import Final

from pydantic import BaseModel

# Fixed Microsoft endpoints for personal Outlook mailboxes (ADR-0025 §1).
OUTLOOK_IMAP_HOST: Final[str] = "outlook.office365.com"
OUTLOOK_IMAP_PORT: Final[int] = 993
OUTLOOK_SMTP_HOST: Final[str] = "smtp-mail.outlook.com"
OUTLOOK_SMTP_PORT: Final[int] = 587

# Delegated scopes — SINGLE-STEP flow (ADR-0025 §5, working Sprint-B config).
#
# The EXPLICIT ``https://outlook.office.com/…`` resource scopes are requested
# DIRECTLY at the authorize + code-exchange step (one ``code -> token`` request,
# no two-step audience upgrade). This is the configuration that synced personal
# Outlook IMAP locally in Sprint B. The P1 (tenant=common) and P2 (two-step
# short-form -> resource-scope refresh) attempts did NOT fix the prod
# "User is authenticated but not connected" symptom and have been reverted.
#
# ``offline_access`` yields the refresh token. ``openid`` + the reserved OIDC
# scopes ``email`` / ``profile`` make the id_token reliably carry an
# email-bearing claim (``email`` / ``preferred_username`` / ``upn`` /
# ``unique_name``) so ``_decode_email_from_id_token`` resolves the mailbox
# address without a Graph call. The same scope set is reused for all refreshes.
#
# NOTE on the host: the OAuth scope *resource* is ``outlook.office.com`` — NOT
# ``outlook.office365.com`` (that is the IMAP/SMTP *connection host*,
# ``OUTLOOK_IMAP_HOST`` above). Using the connection host as a scope resource is
# rejected with ``invalid_scope``.
OUTLOOK_SCOPES: Final[tuple[str, ...]] = (
    "https://outlook.office.com/IMAP.AccessAsUser.All",
    "https://outlook.office.com/SMTP.Send",
    "offline_access",
    "openid",
    "email",
    "profile",
)

# Seconds of head-room before access-token expiry at which we proactively
# refresh (ADR-0025 §3 step 1).
ACCESS_TOKEN_REFRESH_BUFFER_SECONDS: Final[int] = 60

# Redis key prefixes.
OAUTH_STATE_KEY_PREFIX: Final[str] = "oauth_state:"
OAUTH_REFRESH_LOCK_PREFIX: Final[str] = "oauth_refresh_lock:"
OAUTH_REFRESH_LOCK_TTL_SECONDS: Final[int] = 30


class OAuthAuthorizeResponse(BaseModel):
    """Body of ``GET /api/oauth/outlook/authorize`` (ADR-0025 §2).

    ``authorize_url`` is shown to the user as a link (open in the right
    OctoBrowser profile) — we deliberately do NOT 302-redirect. ``state`` is
    echoed for clients that want to display / track it.
    """

    authorize_url: str
    state: str


class OAuthState(BaseModel):
    """Server-side state stored in Redis under ``oauth_state:{state}``."""

    user_id: int
    code_verifier: str
