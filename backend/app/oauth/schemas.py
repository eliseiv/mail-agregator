"""Pydantic schemas + constants for the OAuth Outlook module (ADR-0025)."""

from __future__ import annotations

from typing import Final

from pydantic import BaseModel

# Fixed Microsoft endpoints for personal Outlook mailboxes (ADR-0025 §1).
OUTLOOK_IMAP_HOST: Final[str] = "outlook.office365.com"
OUTLOOK_IMAP_PORT: Final[int] = 993
OUTLOOK_SMTP_HOST: Final[str] = "smtp-mail.outlook.com"
OUTLOOK_SMTP_PORT: Final[int] = 587

# Delegated scopes — TWO-STEP audience-correction flow (ADR-0025 §5, P2-фикс).
#
# WHY TWO SETS: with P1 (tenant=common) consent succeeds and an access_token is
# issued, but personal-Outlook IMAP XOAUTH2 still fails with "User is
# authenticated but not connected". Root cause is an *audience* mismatch — when
# you request the ``https://outlook.office.com/…`` resource scopes DIRECTLY at
# the authorize/code-exchange step for a *personal* Microsoft account, the
# issued access_token does not carry ``aud=https://outlook.office.com`` in the
# form the IMAP/SMTP front-end accepts, so the SASL bind is rejected.
#
# Confirmed workaround (Microsoft Q&A thread #1691402): obtain the refresh
# token using SHORT-form scopes first, then IMMEDIATELY do a refresh_token grant
# asking for the EXPLICIT ``https://outlook.office.com/…`` resource scopes. The
# refresh-issued access_token then carries the correct ``aud`` and IMAP accepts
# it. The scope of the refresh request — not the original consent — determines
# the audience of the issued access_token.
#
# STEP 1 — ``OUTLOOK_AUTHORIZE_SCOPES`` (authorize + code-exchange):
#   SHORT-form scope names (NO ``https://outlook.office.com/`` prefix). These
#   request the same delegated permissions (Microsoft resolves the bare
#   ``IMAP.AccessAsUser.All`` / ``SMTP.Send`` against the Outlook resource) and
#   are the set the user consents to. ``offline_access`` yields the refresh
#   token; ``openid`` + the reserved OIDC scopes ``email`` / ``profile`` make the
#   id_token reliably carry an email-bearing claim
#   (``email`` / ``preferred_username`` / ``upn`` / ``unique_name``) so
#   ``_decode_email_from_id_token`` resolves the mailbox address without a Graph
#   call. OIDC scopes are allowed here because step 1 is addressed to the
#   identity endpoint.
#
# STEP 2 — ``OUTLOOK_RESOURCE_SCOPES`` (immediate upgrade-refresh + all later
#   refreshes): the EXPLICIT ``https://outlook.office.com/…`` resource scopes
#   that pin ``aud=outlook.office.com`` on the issued access_token.
#   ``offline_access`` is kept so the refresh token survives rotation. We must
#   NOT add ``openid`` / ``email`` / ``profile`` here: mixing the reserved OIDC
#   scopes with explicit resource scopes in a *refresh* request triggers
#   ``invalid_scope``. The email is already known from step 1's id_token.
#
# NOTE on the host: the OAuth scope *resource* is ``outlook.office.com`` — NOT
# ``outlook.office365.com`` (that is the IMAP/SMTP *connection host*,
# ``OUTLOOK_IMAP_HOST`` above). Using the connection host as a scope resource is
# rejected with ``invalid_scope``.
OUTLOOK_AUTHORIZE_SCOPES: Final[tuple[str, ...]] = (
    "IMAP.AccessAsUser.All",
    "SMTP.Send",
    "offline_access",
    "openid",
    "email",
    "profile",
)

OUTLOOK_RESOURCE_SCOPES: Final[tuple[str, ...]] = (
    "https://outlook.office.com/IMAP.AccessAsUser.All",
    "https://outlook.office.com/SMTP.Send",
    "offline_access",
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
