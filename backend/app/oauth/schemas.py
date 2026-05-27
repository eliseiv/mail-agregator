"""Pydantic schemas + constants for the OAuth Outlook module (ADR-0025)."""

from __future__ import annotations

from typing import Final

from pydantic import BaseModel

# Fixed Microsoft endpoints for personal Outlook mailboxes (ADR-0025 §1).
OUTLOOK_IMAP_HOST: Final[str] = "outlook.office365.com"
OUTLOOK_IMAP_PORT: Final[int] = 993
OUTLOOK_SMTP_HOST: Final[str] = "smtp-mail.outlook.com"
OUTLOOK_SMTP_PORT: Final[int] = 587

# Minimal delegated scopes (ADR-0025 §5). ``offline_access`` is required to
# obtain a refresh token; ``openid email profile`` let us read the mailbox
# address from the id_token without a Graph call.
OUTLOOK_SCOPES: Final[tuple[str, ...]] = (
    "https://outlook.office365.com/IMAP.AccessAsUser.All",
    "https://outlook.office365.com/SMTP.Send",
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
