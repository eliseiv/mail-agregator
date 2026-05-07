"""Telegram bot launcher (ADR-0018).

Minimal-surface webhook receiver: bot acts solely as launcher — ``/start``
returns an inline keyboard with a single WebApp button pointing at
``TELEGRAM_WEBAPP_URL`` (the main service URL); ``/help`` returns a short
text. No DB tables, no auth changes, no Telegram-side identity binding.
See ``docs/adr/ADR-0018-telegram-launcher.md`` and module 18 in
``docs/05-modules.md``.
"""
