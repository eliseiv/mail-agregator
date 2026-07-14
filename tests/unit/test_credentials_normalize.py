"""Prod-bug regression (2026-07-15) — normalisation of the OPTIONAL SMTP creds.

Source of truth: ``shared/credentials.py`` (``normalize_optional_login`` /
``normalize_optional_secret``) + the module docstring that records the incident:
41 of 114 ``mail_accounts`` rows carried the literal four-character text
``'None'`` in ``smtp_username``; the ``smtp_username or email`` fallback then
LOGGED IN as ``None`` and every send died with ``535 BadCredentials`` while IMAP
(which uses ``email``) kept working.

This module covers the two PURE layers of the fix, no I/O:

- the sentinel matrix of the two normalisers themselves;
- the SCHEMA boundary (``OptionalSmtpCredsMixin`` on the ``accounts`` +
  ``external`` request schemas) — a garbage ``smtp_username`` is scrubbed to
  ``None`` at parse time (case 7, schema half), the secret is only scrubbed on
  the same sentinels but a surviving secret is kept VERBATIM;
- ``ExternalMailboxUpdateRequest.has_account_fields`` — a PATCH carrying ONLY a
  garbage login is NOT an account change (case 7, ``has_account_fields`` half);
- the outbound DTO (``accounts.service._to_dto``) — a stored ``'None'`` is never
  echoed back as if it were a login (case 8).

The send-path resolution (the LOGIN actually handed to the SMTP transport) and
the probe==send equivalence live in ``test_send_credential_resolution.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from backend.app.accounts.schemas import (
    MailAccountCreateRequest,
    MailAccountDTO,
    MailAccountTestRequest,
    MailAccountUpdateRequest,
)
from backend.app.accounts.service import _to_dto
from backend.app.external.schemas import (
    ExternalMailboxCreateRequest,
    ExternalMailboxTestRequest,
    ExternalMailboxUpdateRequest,
)
from shared.credentials import normalize_optional_login, normalize_optional_secret
from shared.models import MailAccount, User

pytestmark = pytest.mark.unit


# The exact set of texts a serialised absent value can take: Python ``str(None)``,
# JSON ``null`` / JS ``undefined`` — in every case, and blank/whitespace.
_ABSENT_LOGINS = [
    None,
    "",
    "   ",
    "\t",
    "\n ",
    "None",
    "none",
    "NONE",
    "NoNe",
    "null",
    "NULL",
    "undefined",
    "UNDEFINED",
    "  None  ",
    "  null ",
]


# ===========================================================================
# 1. normalize_optional_login — identifiers, trimmed
# ===========================================================================


class TestNormalizeOptionalLogin:
    @pytest.mark.parametrize("value", _ABSENT_LOGINS)
    def test_absence_sentinels_become_none(self, value: str | None) -> None:
        assert normalize_optional_login(value) is None

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("real@login", "real@login"),
            ("  real@login  ", "real@login"),  # a login IS trimmed
            ("user", "user"),
            ("noneofyourbusiness", "noneofyourbusiness"),  # substring, not the sentinel
            ("null-ish-login", "null-ish-login"),
            ("None Smith", "None Smith"),  # not equal to the sentinel after strip
        ],
    )
    def test_real_logins_survive(self, value: str, expected: str) -> None:
        assert normalize_optional_login(value) == expected


# ===========================================================================
# 2. normalize_optional_secret — opaque, NEVER trimmed
# ===========================================================================


class TestNormalizeOptionalSecret:
    @pytest.mark.parametrize("value", _ABSENT_LOGINS)
    def test_absence_sentinels_become_none(self, value: str | None) -> None:
        assert normalize_optional_secret(value) is None

    @pytest.mark.parametrize(
        "value",
        [
            "s3cret",
            "  s3cret  ",  # leading/trailing spaces are SIGNIFICANT in a secret
            " x ",  # a secret whose CONTENT is non-blank; padding kept
            "p@ss None word",  # contains the sentinel substring but is a real pw
            "\tpwd\t",  # surrounding tabs kept verbatim
        ],
    )
    def test_real_secrets_are_returned_verbatim(self, value: str) -> None:
        # Everything here has non-blank content and is not a bare sentinel, so it
        # survives — and is returned EXACTLY (including any surrounding whitespace).
        assert normalize_optional_secret(value) == value

    def test_spaces_around_secret_are_not_stripped(self) -> None:
        # The single most load-bearing difference from the login normaliser.
        assert normalize_optional_secret("  keep me  ") == "  keep me  "
        assert normalize_optional_login("  keep me  ") == "keep me"


# ===========================================================================
# 3. Schema boundary — smtp_username/smtp_password scrubbed at parse time (case 7)
# ===========================================================================

_FULL_CREDS: dict[str, object] = {
    "email": "box@example.com",
    "password": "imap-pw",
    "imap_host": "imap.example.com",
    "imap_port": 993,
    "imap_ssl": True,
    "smtp_host": "smtp.example.com",
    "smtp_port": 465,
    "smtp_ssl": True,
    "smtp_starttls": False,
}


class TestSchemaScrubsGarbageLogin:
    """A ``'None'`` / blank ``smtp_username`` never reaches the row again."""

    def test_accounts_create_request_scrubs_none(self) -> None:
        req = MailAccountCreateRequest(**_FULL_CREDS, smtp_username="None")
        assert req.smtp_username is None

    def test_accounts_test_request_scrubs_none(self) -> None:
        req = MailAccountTestRequest(**_FULL_CREDS, smtp_username="None")
        assert req.smtp_username is None

    def test_accounts_update_request_scrubs_none(self) -> None:
        req = MailAccountUpdateRequest(smtp_username="None")
        assert req.smtp_username is None

    def test_external_test_request_scrubs_none(self) -> None:
        req = ExternalMailboxTestRequest(**_FULL_CREDS, smtp_username="None")
        assert req.smtp_username is None

    def test_external_create_request_scrubs_none(self) -> None:
        req = ExternalMailboxCreateRequest(**_FULL_CREDS, smtp_username="None")
        assert req.smtp_username is None

    def test_external_update_request_scrubs_none(self) -> None:
        req = ExternalMailboxUpdateRequest(smtp_username="None")
        assert req.smtp_username is None

    @pytest.mark.parametrize("garbage", ["", "   ", "null", "NONE", "undefined"])
    def test_the_whole_sentinel_family_is_scrubbed_on_the_external_create(
        self, garbage: str
    ) -> None:
        req = ExternalMailboxCreateRequest(**_FULL_CREDS, smtp_username=garbage)
        assert req.smtp_username is None

    def test_real_login_passes_the_schema_untouched(self) -> None:
        req = ExternalMailboxCreateRequest(**_FULL_CREDS, smtp_username="real@login")
        assert req.smtp_username == "real@login"

    def test_schema_scrubs_garbage_secret_but_keeps_a_real_one_verbatim(self) -> None:
        # secret sentinel -> None
        assert MailAccountUpdateRequest(smtp_password="None").smtp_password is None
        # real secret with significant spaces -> kept exactly
        assert MailAccountUpdateRequest(smtp_password="  s3cret  ").smtp_password == "  s3cret  "


# ===========================================================================
# 4. has_account_fields — a garbage-only login PATCH is NOT a change (case 7)
# ===========================================================================


class TestHasAccountFieldsWithGarbageLogin:
    def test_patch_with_only_garbage_smtp_username_is_not_an_account_change(self) -> None:
        # 'None' is scrubbed to None by the mixin -> smtp_username is None ->
        # nothing was actually submitted -> has_account_fields is False, so the
        # write-service takes the empty-PATCH branch (no credential rewrite).
        req = ExternalMailboxUpdateRequest(smtp_username="None")
        assert req.smtp_username is None
        assert req.has_account_fields is False

    @pytest.mark.parametrize("garbage", ["", "   ", "null", "undefined", "NONE"])
    def test_every_sentinel_only_patch_is_not_an_account_change(self, garbage: str) -> None:
        assert ExternalMailboxUpdateRequest(smtp_username=garbage).has_account_fields is False

    def test_patch_with_a_real_smtp_username_IS_an_account_change(self) -> None:
        req = ExternalMailboxUpdateRequest(smtp_username="real@login")
        assert req.smtp_username == "real@login"
        assert req.has_account_fields is True


# ===========================================================================
# 5. Outbound DTO — a stored 'None' is echoed as null, never as a login (case 8)
# ===========================================================================


def _account(smtp_username: str | None) -> MailAccount:
    now = datetime.now(UTC)
    return MailAccount(
        id=7,
        user_id=1,
        email="box@example.com",
        display_name="Box",
        auth_type="password",
        oauth_needs_consent=False,
        imap_host="imap.example.com",
        imap_port=993,
        imap_ssl=True,
        smtp_host="smtp.example.com",
        smtp_port=465,
        smtp_ssl=True,
        smtp_starttls=False,
        smtp_username=smtp_username,
        is_active=True,
        last_synced_at=None,
        last_sync_error=None,
        consecutive_failures=0,
        created_at=now,
    )


def _owner() -> User:
    return User(id=1, username="crm-service", display_name=None)


class TestDtoNeverEchoesGarbageLogin:
    def test_stored_none_becomes_null_in_the_dto(self) -> None:
        dto: MailAccountDTO = _to_dto(_account("None"), _owner())
        assert dto.smtp_username is None

    @pytest.mark.parametrize("garbage", ["", "   ", "null", "undefined", "NONE"])
    def test_every_sentinel_is_nulled_in_the_dto(self, garbage: str) -> None:
        assert _to_dto(_account(garbage), _owner()).smtp_username is None

    def test_real_login_is_preserved_in_the_dto(self) -> None:
        assert _to_dto(_account("real@login"), _owner()).smtp_username == "real@login"

    def test_a_truly_absent_login_stays_null(self) -> None:
        assert _to_dto(_account(None), _owner()).smtp_username is None
