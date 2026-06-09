"""Unit tests for ADR-0027 §2 config: push-only per-team bot derivation.

Source of truth: ``shared/config.py`` (``PushTeamBot``, ``admin_telegram_ids``,
``push_team_bots``, ``push_team_bots_enabled``, the ``model_validator``
duplicate-group_id fail-fast).

We instantiate :class:`Settings` directly with explicit keyword arguments;
pydantic-settings treats init kwargs as the highest-priority source, so the
ambient ``.env`` does not interfere with the value under test, while the
required secrets are supplied inline so the ``model_validator`` passes.
"""

from __future__ import annotations

import base64

import pytest
from pydantic import ValidationError

from shared.config import PushTeamBot, Settings

pytestmark = pytest.mark.unit

# A valid base64 of exactly 32 bytes for MAIL_ENCRYPTION_KEY.
_VALID_KEY = base64.b64encode(b"\x00" * 32).decode()

# Minimal set of required-in-prod secrets so the model_validator passes.
_REQUIRED = {
    "MAIL_ENCRYPTION_KEY": _VALID_KEY,
    "ADMIN_PASSWORD": "x",
    "S3_ACCESS_KEY": "x",
    "S3_SECRET_KEY": "x",
    "APP_ENV": "dev",
}

# A pair of admin chat ids so push_team_bots is not gated off by an empty
# recipient list (a bot with no recipients is materialised as an empty list).
_ADMINS = "11111111,22222222"


def _settings(**overrides: object) -> Settings:
    return Settings(**{**_REQUIRED, **overrides})  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# admin_telegram_ids — CSV parsing (ADR-0027 §2)
# ---------------------------------------------------------------------------


class TestAdminTelegramIds:
    def test_garbage_and_whitespace_dropped_keeps_ints_in_order(self) -> None:
        # The spec example: "111, 222 ,foo,,333" -> [111, 222, 333].
        s = _settings(ADMIN_TELEGRAM_IDS="111, 222 ,foo,,333")
        assert s.admin_telegram_ids == [111, 222, 333]

    def test_empty_string_yields_empty_list(self) -> None:
        assert _settings(ADMIN_TELEGRAM_IDS="").admin_telegram_ids == []

    def test_negative_chat_ids_are_kept(self) -> None:
        # Telegram group/channel chat ids are negative; the parser keeps them.
        assert _settings(ADMIN_TELEGRAM_IDS="-100123, 55").admin_telegram_ids == [-100123, 55]

    def test_only_garbage_yields_empty_list(self) -> None:
        assert _settings(ADMIN_TELEGRAM_IDS="foo, bar, , -").admin_telegram_ids == []


# ---------------------------------------------------------------------------
# push_team_bots — the configured-bot matrix (ADR-0027 §2)
# ---------------------------------------------------------------------------


class TestPushTeamBotsMatrix:
    """A bot is materialised ONLY when token != "" AND group_id > 0 AND
    admin_telegram_ids is non-empty. We exercise every dimension."""

    @pytest.mark.parametrize(
        ("token", "group_id", "admins", "expected"),
        [
            # token filled / group>0 / admins present -> configured
            ("TOK", 1, _ADMINS, True),
            # token empty -> not configured
            ("", 1, _ADMINS, False),
            # group_id == 0 -> not configured
            ("TOK", 0, _ADMINS, False),
            # admins empty -> whole channel off (no recipients)
            ("TOK", 1, "", False),
            # token empty AND group 0 -> not configured
            ("", 0, _ADMINS, False),
        ],
    )
    def test_single_bot_configured_predicate(
        self, token: str, group_id: int, admins: str, expected: bool
    ) -> None:
        s = _settings(
            BOT_IVAN_TOKEN=token,
            BOT_IVAN_GROUP_ID=group_id,
            ADMIN_TELEGRAM_IDS=admins,
        )
        names = [b.name for b in s.push_team_bots]
        assert ("ivan" in names) is expected

    def test_fully_configured_bot_has_expected_fields(self) -> None:
        s = _settings(
            BOT_IVAN_TOKEN="IVAN_TOK",
            BOT_IVAN_GROUP_ID=1,
            ADMIN_TELEGRAM_IDS=_ADMINS,
        )
        assert s.push_team_bots == [PushTeamBot(name="ivan", token="IVAN_TOK", group_id=1)]

    def test_all_three_configured_distinct_groups(self) -> None:
        s = _settings(
            BOT_IVAN_TOKEN="A",
            BOT_IVAN_GROUP_ID=1,
            BOT_ALEXANDRA_TOKEN="B",
            BOT_ALEXANDRA_GROUP_ID=2,
            BOT_ANDREI_TOKEN="C",
            BOT_ANDREI_GROUP_ID=3,
            ADMIN_TELEGRAM_IDS=_ADMINS,
        )
        assert s.push_team_bots == [
            PushTeamBot(name="ivan", token="A", group_id=1),
            PushTeamBot(name="alexandra", token="B", group_id=2),
            PushTeamBot(name="andrei", token="C", group_id=3),
        ]

    def test_partially_configured_only_returns_configured(self) -> None:
        # ivan configured; alexandra has token but no group; andrei has group
        # but no token -> only ivan is materialised.
        s = _settings(
            BOT_IVAN_TOKEN="A",
            BOT_IVAN_GROUP_ID=1,
            BOT_ALEXANDRA_TOKEN="B",
            BOT_ALEXANDRA_GROUP_ID=0,
            BOT_ANDREI_TOKEN="",
            BOT_ANDREI_GROUP_ID=3,
            ADMIN_TELEGRAM_IDS=_ADMINS,
        )
        assert [b.name for b in s.push_team_bots] == ["ivan"]

    def test_no_admins_returns_empty_even_with_configured_tokens(self) -> None:
        s = _settings(
            BOT_IVAN_TOKEN="A",
            BOT_IVAN_GROUP_ID=1,
            ADMIN_TELEGRAM_IDS="",
        )
        assert s.push_team_bots == []


# ---------------------------------------------------------------------------
# push_team_bots_enabled (ADR-0027 §2)
# ---------------------------------------------------------------------------


class TestPushTeamBotsEnabled:
    def test_enabled_when_a_bot_and_admins_present(self) -> None:
        s = _settings(BOT_IVAN_TOKEN="A", BOT_IVAN_GROUP_ID=1, ADMIN_TELEGRAM_IDS=_ADMINS)
        assert s.push_team_bots_enabled is True

    def test_disabled_when_no_admins(self) -> None:
        s = _settings(BOT_IVAN_TOKEN="A", BOT_IVAN_GROUP_ID=1, ADMIN_TELEGRAM_IDS="")
        assert s.push_team_bots_enabled is False

    def test_disabled_when_no_bot_configured(self) -> None:
        s = _settings(ADMIN_TELEGRAM_IDS=_ADMINS)
        assert s.push_team_bots_enabled is False

    def test_default_disabled(self) -> None:
        assert _settings().push_team_bots_enabled is False


# ---------------------------------------------------------------------------
# model_validator fail-fast: duplicate group_id (ADR-0027 §2 invariant)
# ---------------------------------------------------------------------------


class TestDuplicateGroupIdFailFast:
    def test_two_configured_bots_same_group_id_raises(self) -> None:
        with pytest.raises(ValidationError, match="Duplicate push-bot group_id"):
            _settings(
                BOT_IVAN_TOKEN="A",
                BOT_IVAN_GROUP_ID=5,
                BOT_ALEXANDRA_TOKEN="B",
                BOT_ALEXANDRA_GROUP_ID=5,
                ADMIN_TELEGRAM_IDS=_ADMINS,
            )

    def test_empty_token_bot_on_same_group_id_is_not_an_error(self) -> None:
        # alexandra shares ivan's group_id but has an EMPTY token -> it is not
        # "configured" and does NOT participate in the duplicate check.
        s = _settings(
            BOT_IVAN_TOKEN="A",
            BOT_IVAN_GROUP_ID=5,
            BOT_ALEXANDRA_TOKEN="",
            BOT_ALEXANDRA_GROUP_ID=5,
            ADMIN_TELEGRAM_IDS=_ADMINS,
        )
        # Only ivan is configured -> no error and exactly one bot.
        assert [b.name for b in s.push_team_bots] == ["ivan"]

    def test_group_id_zero_collision_is_not_an_error(self) -> None:
        # Two bots both with group_id 0 (default) and tokens set are NOT
        # configured (group_id must be > 0) -> no duplicate error.
        s = _settings(
            BOT_IVAN_TOKEN="A",
            BOT_IVAN_GROUP_ID=0,
            BOT_ALEXANDRA_TOKEN="B",
            BOT_ALEXANDRA_GROUP_ID=0,
            ADMIN_TELEGRAM_IDS=_ADMINS,
        )
        assert s.push_team_bots == []

    def test_distinct_group_ids_pass(self) -> None:
        s = _settings(
            BOT_IVAN_TOKEN="A",
            BOT_IVAN_GROUP_ID=1,
            BOT_ALEXANDRA_TOKEN="B",
            BOT_ALEXANDRA_GROUP_ID=2,
            ADMIN_TELEGRAM_IDS=_ADMINS,
        )
        assert len(s.push_team_bots) == 2


# ---------------------------------------------------------------------------
# Field bounds + defaults for the new knobs (ADR-0027 §2)
# ---------------------------------------------------------------------------


class TestNewKnobs:
    def test_dispatch_interval_default(self) -> None:
        assert _settings().PUSH_NOTIFY_DISPATCH_INTERVAL_SECONDS == 5

    def test_batch_size_default(self) -> None:
        assert _settings().PUSH_NOTIFY_BATCH_SIZE == 30

    @pytest.mark.parametrize("val", [-1, 0])
    def test_group_id_negative_rejected(self, val: int) -> None:
        # BOT_*_GROUP_ID has ge=0; a negative value is rejected by pydantic.
        if val < 0:
            with pytest.raises(ValidationError):
                _settings(BOT_IVAN_GROUP_ID=val)
        else:
            # 0 is the valid "unset" default.
            assert _settings(BOT_IVAN_GROUP_ID=0).BOT_IVAN_GROUP_ID == 0
