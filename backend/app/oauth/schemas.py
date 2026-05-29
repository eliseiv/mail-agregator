"""Pydantic schemas + constants for the OAuth Outlook module (ADR-0025)."""

from __future__ import annotations

from typing import Final

from pydantic import BaseModel

# Fixed Microsoft endpoints for personal Outlook mailboxes (ADR-0025 §1).
OUTLOOK_IMAP_HOST: Final[str] = "outlook.office365.com"
OUTLOOK_IMAP_PORT: Final[int] = 993
OUTLOOK_SMTP_HOST: Final[str] = "smtp-mail.outlook.com"
OUTLOOK_SMTP_PORT: Final[int] = 587

# Minimal delegated scopes (ADR-0025 §5).
#
# IMPORTANT: the OAuth *scope resource* for Outlook IMAP/SMTP is
# ``outlook.office.com`` — NOT ``outlook.office365.com``. The latter is the
# IMAP/SMTP *connection host* (outlook.office365.com:993, see
# ``OUTLOOK_IMAP_HOST`` above), but Microsoft rejects it as a scope resource
# with ``invalid_scope`` ("The provided resource value for the input
# parameter 'scope' is not valid.").
#
# ``offline_access`` is required to obtain a refresh token. ``openid`` yields
# an id_token; we ALSO request the reserved OIDC scopes ``email`` and
# ``profile`` so the id_token reliably carries an email-bearing claim
# (``email`` / ``preferred_username`` / ``upn`` / ``unique_name``) for personal
# Microsoft accounts — ``_decode_email_from_id_token`` reads those to resolve
# the mailbox address without a Graph call.
#
# NOTE: ``email`` / ``profile`` are reserved OpenID Connect scopes addressed to
# the *identity* endpoint, not the Graph resource — adding them alongside the
# ``outlook.office.com`` resource scopes does NOT trigger ``invalid_scope``
# (real-world consent succeeded with this exact set). The earlier
# ``invalid_scope`` was caused ONLY by the wrong resource host
# (``outlook.office365.com`` -> ``outlook.office.com``), not by these scopes.
# Dropping ``email``/``profile`` left the id_token without an email claim for
# personal accounts, yielding "Could not resolve mailbox email" — hence they
# are restored here.
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
