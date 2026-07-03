"""Unit tests for ``backend.app.accounts.providers``.

The provider table is just a small, deterministic mapping of email domains
to IMAP/SMTP defaults; we verify each well-known domain returns the
expected preset and that unknown / malformed addresses return ``None``.

Source of truth: ``backend/app/accounts/providers.py`` +
``docs/05-modules.md`` sec. 9.
"""

from __future__ import annotations

import pytest

from backend.app.accounts.providers import (
    ProviderHint,
    suggest_provider_defaults,
)

pytestmark = pytest.mark.unit


class TestKnownDomains:
    """Each well-known domain should resolve to a preset whose hosts and
    transport options match the public docs of the provider.
    """

    def test_gmail_returns_imap_gmail_with_ssl(self) -> None:
        # ADR-0032 follow-up: prod host blocks outbound :465 → SMTP on
        # :587/STARTTLS for all password providers (IMAP stays :993 SSL).
        hint = suggest_provider_defaults("alice@gmail.com")
        assert hint == ProviderHint(
            imap_host="imap.gmail.com",
            imap_port=993,
            imap_ssl=True,
            smtp_host="smtp.gmail.com",
            smtp_port=587,
            smtp_ssl=False,
            smtp_starttls=True,
        )

    @pytest.mark.parametrize(
        "domain,imap_host,smtp_host",
        [
            ("aol.com", "imap.aol.com", "smtp.aol.com"),
            ("yahoo.com", "imap.mail.yahoo.com", "smtp.mail.yahoo.com"),
        ],
    )
    def test_aol_yahoo_presets_use_587_starttls(
        self, domain: str, imap_host: str, smtp_host: str
    ) -> None:
        hint = suggest_provider_defaults(f"user@{domain}")
        assert hint is not None
        assert hint.imap_host == imap_host
        assert hint.imap_ssl is True
        assert hint.smtp_host == smtp_host
        assert hint.smtp_port == 587
        assert hint.smtp_ssl is False
        assert hint.smtp_starttls is True

    def test_googlemail_alias_matches_gmail(self) -> None:
        a = suggest_provider_defaults("a@gmail.com")
        b = suggest_provider_defaults("a@googlemail.com")
        assert a == b

    def test_yandex_ru_uses_imap_yandex_ru(self) -> None:
        hint = suggest_provider_defaults("user@yandex.ru")
        assert hint is not None
        assert hint.imap_host == "imap.yandex.ru"
        assert hint.smtp_host == "smtp.yandex.ru"
        assert hint.imap_ssl is True

    def test_yandex_com_uses_imap_yandex_com(self) -> None:
        hint = suggest_provider_defaults("user@yandex.com")
        assert hint is not None
        assert hint.imap_host == "imap.yandex.com"
        assert hint.smtp_host == "smtp.yandex.com"

    @pytest.mark.parametrize("domain", ["mail.ru", "inbox.ru", "bk.ru", "list.ru"])
    def test_mail_ru_family_shares_servers(self, domain: str) -> None:
        hint = suggest_provider_defaults(f"user@{domain}")
        assert hint is not None
        assert hint.imap_host == "imap.mail.ru"
        assert hint.smtp_host == "smtp.mail.ru"

    @pytest.mark.parametrize("domain", ["outlook.com", "hotmail.com", "live.com"])
    def test_outlook_family_uses_office365_with_starttls(self, domain: str) -> None:
        hint = suggest_provider_defaults(f"user@{domain}")
        assert hint is not None
        assert hint.imap_host == "outlook.office365.com"
        assert hint.smtp_host == "smtp.office365.com"
        # Outlook uses STARTTLS on 587, NOT SMTPS on 465.
        assert hint.smtp_port == 587
        assert hint.smtp_ssl is False
        assert hint.smtp_starttls is True


class TestCaseInsensitivity:
    def test_uppercase_domain_matches(self) -> None:
        # Users frequently type their email with mixed case; we lower-case
        # the domain part before lookup.
        assert suggest_provider_defaults("Alice@GMAIL.COM") == suggest_provider_defaults(
            "alice@gmail.com"
        )

    def test_mixed_case_local_part_does_not_affect_lookup(self) -> None:
        # Local part case is irrelevant for routing; the lookup is by
        # domain only.
        a = suggest_provider_defaults("ALICE@gmail.com")
        b = suggest_provider_defaults("alice@gmail.com")
        assert a == b


class TestUnknownAndMalformed:
    def test_unknown_domain_returns_none(self) -> None:
        assert suggest_provider_defaults("user@unknown-provider.test") is None

    def test_empty_string_returns_none(self) -> None:
        assert suggest_provider_defaults("") is None

    def test_missing_at_sign_returns_none(self) -> None:
        assert suggest_provider_defaults("not-an-email") is None

    def test_only_at_returns_none(self) -> None:
        # ``"@"`` parses to empty domain; not in table.
        assert suggest_provider_defaults("@") is None


class TestProviderHintImmutability:
    def test_hint_is_frozen_dataclass(self) -> None:
        # ProviderHint is declared frozen=True; mutating must raise.
        hint = suggest_provider_defaults("alice@gmail.com")
        assert hint is not None
        with pytest.raises((AttributeError, TypeError, Exception)):  # FrozenInstanceError
            hint.imap_host = "evil.example.com"  # type: ignore[misc]
