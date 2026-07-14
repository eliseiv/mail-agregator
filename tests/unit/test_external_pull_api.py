"""Unit tests for the external PULL-API helpers + schemas (ADR-0029).

Pure, no-I/O coverage of:

- ``backend.app.external.router._bearer`` — Authorization header parsing.
- ``backend.app.external.router._api_key_matches`` — constant-time compare,
  feature-off short-circuit.
- ``ExternalMessage*`` Pydantic schemas — field whitelist / nullability /
  ``to_addrs`` always-string contract (ADR-0029 §2/§6) and the ABSENCE of the
  ``tags`` field after the decommission (ADR-0044 §4 phase A1: tags are gone —
  the matching logic moved to the CRM).

The auth-flow / keyset / canonical-dedup behaviours are covered end-to-end in
``tests/integration/external/test_external_pull_api.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from backend.app.external.router import _api_key_matches, _bearer
from backend.app.external.schemas import (
    ExternalMailAccountDTO,
    ExternalMessageDTO,
    ExternalMessagesPage,
    ExternalMessagesResponse,
)


class TestBearerParsing:
    @pytest.mark.parametrize(
        ("header", "expected"),
        [
            ("Bearer abc123", "abc123"),
            ("bearer abc123", "abc123"),  # case-insensitive scheme
            ("BEARER abc123", "abc123"),
            ("Bearer   spaced  ", "spaced"),  # token trimmed
            (None, None),
            ("", None),
            ("abc123", None),  # no scheme
            ("Basic abc123", None),  # wrong scheme
            ("Bearer", None),  # no token
            ("Bearer ", None),  # empty token after trim
            ("Bearer    ", None),
        ],
    )
    def test_bearer_extraction(self, header: str | None, expected: str | None) -> None:
        assert _bearer(header) == expected

    def test_bearer_keeps_internal_spaces_in_token_after_first_split(self) -> None:
        # Only the first space splits scheme/token; the token keeps the rest
        # (then stripped at the edges).
        assert _bearer("Bearer a b c") == "a b c"


class TestApiKeyMatch:
    def test_correct_key_matches(self) -> None:
        assert _api_key_matches("secret", "secret") is True

    def test_wrong_key_does_not_match(self) -> None:
        assert _api_key_matches("nope", "secret") is False

    def test_empty_expected_feature_off_never_matches(self) -> None:
        # Feature off (expected="") must always be False, even for an empty
        # provided value — config never "accidentally on". ADR-0029 §4.
        assert _api_key_matches("", "") is False
        assert _api_key_matches("anything", "") is False

    def test_length_mismatch_does_not_match(self) -> None:
        assert _api_key_matches("short", "a-much-longer-expected-key") is False


class TestSchemas:
    def _msg(self, **over: object) -> ExternalMessageDTO:
        base: dict[str, object] = {
            "id": 1,
            "subject": "Hi",
            "internal_date": datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
            "from_addr": "a@x.com",
            "from_name": "A",
            "to_addrs": "b@x.com",
            "cc_addrs": None,
            "mail_account": ExternalMailAccountDTO(id=9, email="m@x.com", display_name="M"),
            "body_text": "body",
            "body_html": None,
            "body_present": True,
            "body_truncated": False,
        }
        base.update(over)
        return ExternalMessageDTO(**base)  # type: ignore[arg-type]

    def test_mail_account_dto_whitelist(self) -> None:
        dto = ExternalMailAccountDTO(id=1, email="e@x.com", display_name=None)
        dumped = dto.model_dump()
        assert set(dumped.keys()) == {"id", "email", "display_name"}

    def test_message_dto_field_whitelist(self) -> None:
        dumped = self._msg().model_dump()
        assert set(dumped.keys()) == {
            "id",
            "subject",
            "internal_date",
            "from_addr",
            "from_name",
            "to_addrs",
            "cc_addrs",
            "mail_account",
            "body_text",
            "body_html",
            "body_present",
            "body_truncated",
        }
        # No secret / internal columns leak via the DTO.
        for forbidden in ("uid", "uidvalidity", "encrypted_password", "mail_account_id", "user_id"):
            assert forbidden not in dumped

    def test_message_dto_has_no_tags_field_after_decommission(self) -> None:
        # ADR-0044 §4 (phase A1) / §1: tags are DROPPED — the pull DTO must not
        # carry a ``tags`` key any more (the CRM owns tag matching).
        assert "tags" not in self._msg().model_dump()
        assert "tags" not in ExternalMessageDTO.model_fields

    def test_mailbox_dto_has_no_group_field_after_decommission(self) -> None:
        # ADR-0044 §4 (phase A1): ``group_id`` went away with teams/groups.
        from backend.app.external.schemas import ExternalMailboxDTO

        assert "group_id" not in ExternalMailboxDTO.model_fields
        assert "group" not in ExternalMailboxDTO.model_fields

    def test_nullable_fields_accept_none(self) -> None:
        dumped = self._msg(subject=None, from_name=None, cc_addrs=None, body_html=None).model_dump()
        assert dumped["subject"] is None
        assert dumped["from_name"] is None
        assert dumped["cc_addrs"] is None
        assert dumped["body_html"] is None

    def test_to_addrs_is_always_string(self) -> None:
        dumped = self._msg(to_addrs="").model_dump()
        assert isinstance(dumped["to_addrs"], str)
        assert dumped["to_addrs"] == ""

    def test_page_envelope_shape(self) -> None:
        page = ExternalMessagesResponse(messages=[self._msg()], next_since_id=1, has_more=True)
        dumped = page.model_dump()
        assert set(dumped.keys()) == {"messages", "next_since_id", "has_more"}
        assert dumped["next_since_id"] == 1
        assert dumped["has_more"] is True

    def test_page_alias_is_the_same_class(self) -> None:
        # ADR-0029 §6 alias must stay importable + identical.
        assert ExternalMessagesPage is ExternalMessagesResponse
