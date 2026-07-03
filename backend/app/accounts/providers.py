"""Provider auto-suggest table — IMAP/SMTP defaults for popular domains.

Single source of truth for backend; the frontend ``account_form.js`` keeps a
shorter mirror table, but both POST/test ultimately validate against the
real IMAP/SMTP servers, so drift between the two is harmless.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ProviderHint:
    imap_host: str
    imap_port: int
    imap_ssl: bool
    smtp_host: str
    smtp_port: int
    smtp_ssl: bool
    smtp_starttls: bool


# Hand-curated list per ``docs/05-modules.md`` sec. 9.
#
# ADR-0032 follow-up: the prod host (Hetzner) blocks outbound TCP :465, but
# :587/STARTTLS works. IMAP stays on :993 SSL for every provider; SMTP for all
# password providers is now :587 (``smtp_ssl=False``, ``smtp_starttls=True``).
# 13 domains total (QA §21): the 8 password providers below + outlook/hotmail/
# live (already 587) + the new ``aol.com`` / ``yahoo.com`` presets.
_PROVIDERS: dict[str, ProviderHint] = {
    "gmail.com": ProviderHint(
        imap_host="imap.gmail.com",
        imap_port=993,
        imap_ssl=True,
        smtp_host="smtp.gmail.com",
        smtp_port=587,
        smtp_ssl=False,
        smtp_starttls=True,
    ),
    "googlemail.com": ProviderHint(
        imap_host="imap.gmail.com",
        imap_port=993,
        imap_ssl=True,
        smtp_host="smtp.gmail.com",
        smtp_port=587,
        smtp_ssl=False,
        smtp_starttls=True,
    ),
    "yandex.ru": ProviderHint(
        imap_host="imap.yandex.ru",
        imap_port=993,
        imap_ssl=True,
        smtp_host="smtp.yandex.ru",
        smtp_port=587,
        smtp_ssl=False,
        smtp_starttls=True,
    ),
    "yandex.com": ProviderHint(
        imap_host="imap.yandex.com",
        imap_port=993,
        imap_ssl=True,
        smtp_host="smtp.yandex.com",
        smtp_port=587,
        smtp_ssl=False,
        smtp_starttls=True,
    ),
    "mail.ru": ProviderHint(
        imap_host="imap.mail.ru",
        imap_port=993,
        imap_ssl=True,
        smtp_host="smtp.mail.ru",
        smtp_port=587,
        smtp_ssl=False,
        smtp_starttls=True,
    ),
    "inbox.ru": ProviderHint(
        imap_host="imap.mail.ru",
        imap_port=993,
        imap_ssl=True,
        smtp_host="smtp.mail.ru",
        smtp_port=587,
        smtp_ssl=False,
        smtp_starttls=True,
    ),
    "bk.ru": ProviderHint(
        imap_host="imap.mail.ru",
        imap_port=993,
        imap_ssl=True,
        smtp_host="smtp.mail.ru",
        smtp_port=587,
        smtp_ssl=False,
        smtp_starttls=True,
    ),
    "list.ru": ProviderHint(
        imap_host="imap.mail.ru",
        imap_port=993,
        imap_ssl=True,
        smtp_host="smtp.mail.ru",
        smtp_port=587,
        smtp_ssl=False,
        smtp_starttls=True,
    ),
    "aol.com": ProviderHint(
        imap_host="imap.aol.com",
        imap_port=993,
        imap_ssl=True,
        smtp_host="smtp.aol.com",
        smtp_port=587,
        smtp_ssl=False,
        smtp_starttls=True,
    ),
    "yahoo.com": ProviderHint(
        imap_host="imap.mail.yahoo.com",
        imap_port=993,
        imap_ssl=True,
        smtp_host="smtp.mail.yahoo.com",
        smtp_port=587,
        smtp_ssl=False,
        smtp_starttls=True,
    ),
    "outlook.com": ProviderHint(
        imap_host="outlook.office365.com",
        imap_port=993,
        imap_ssl=True,
        smtp_host="smtp.office365.com",
        smtp_port=587,
        smtp_ssl=False,
        smtp_starttls=True,
    ),
    "hotmail.com": ProviderHint(
        imap_host="outlook.office365.com",
        imap_port=993,
        imap_ssl=True,
        smtp_host="smtp.office365.com",
        smtp_port=587,
        smtp_ssl=False,
        smtp_starttls=True,
    ),
    "live.com": ProviderHint(
        imap_host="outlook.office365.com",
        imap_port=993,
        imap_ssl=True,
        smtp_host="smtp.office365.com",
        smtp_port=587,
        smtp_ssl=False,
        smtp_starttls=True,
    ),
}


def suggest_provider_defaults(email: str) -> ProviderHint | None:
    """Return defaults for the email's domain (case-insensitive), else None."""
    if not email or "@" not in email:
        return None
    domain = email.rsplit("@", 1)[1].lower()
    return _PROVIDERS.get(domain)
