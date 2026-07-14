"""OAuth2 Outlook module (ADR-0025 / ADR-0045).

ADR-0044 §7: the session consent router went away with the UI; the headless
consent flow lives in ``backend/app/external/router.py`` (ADR-0045). This module
keeps ``OutlookOAuthService`` (authorize URL + code exchange) and
``OutlookTokenService`` (refresh → access token for the worker sync and the SMTP
send). A linked account is an ordinary ``mail_accounts`` row with
``auth_type='oauth_outlook'``.
"""
