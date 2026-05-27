"""OAuth2 Outlook module (ADR-0025, Sprint B).

Serves the consent flow (``/api/oauth/outlook/authorize`` +
``/api/oauth/outlook/callback``) and the token-refresh helper used by the
worker (before IMAP) and the send/test paths (before SMTP). A linked account
is an ordinary ``mail_accounts`` row with ``auth_type='oauth_outlook'``.
"""
