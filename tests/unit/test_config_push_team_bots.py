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
        assert s.push_team_bots == [
            PushTeamBot(name="ivan", token="IVAN_TOK", group_id=1, webhook_secret="")
        ]

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
            PushTeamBot(name="ivan", token="A", group_id=1, webhook_secret=""),
            PushTeamBot(name="alexandra", token="B", group_id=2, webhook_secret=""),
            PushTeamBot(name="andrei", token="C", group_id=3, webhook_secret=""),
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
# round-42 (ADR-0027 §2/§7/§10) — webhook_secret field on PushTeamBot
# ---------------------------------------------------------------------------


class TestPushTeamBotWebhookSecret:
    def test_secret_is_carried_through_to_push_team_bot(self) -> None:
        s = _settings(
            BOT_IVAN_TOKEN="IVAN_TOK",
            BOT_IVAN_GROUP_ID=1,
            BOT_IVAN_WEBHOOK_SECRET="deadbeefdeadbeefdeadbeefdeadbeef",
            ADMIN_TELEGRAM_IDS=_ADMINS,
        )
        assert s.push_team_bots == [
            PushTeamBot(
                name="ivan",
                token="IVAN_TOK",
                group_id=1,
                webhook_secret="deadbeefdeadbeefdeadbeefdeadbeef",
            )
        ]

    def test_bot_stays_in_list_with_empty_secret(self) -> None:
        # A configured bot with NO webhook secret is still materialised — it
        # still delivers notifications; only the callback button + push-webhook
        # route deactivate (graceful degradation, ADR-0027 §2/§7 round-42).
        s = _settings(
            BOT_IVAN_TOKEN="IVAN_TOK",
            BOT_IVAN_GROUP_ID=1,
            BOT_IVAN_WEBHOOK_SECRET="",
            ADMIN_TELEGRAM_IDS=_ADMINS,
        )
        ivan = next(b for b in s.push_team_bots if b.name == "ivan")
        assert ivan.webhook_secret == ""

    def test_with_button_predicate_matches_bool_secret(self) -> None:
        # ADR-0027 §7: the dispatcher attaches the button iff bool(secret).
        # We assert the predicate the worker uses (``with_button = bool(secret)``)
        # tracks the config exactly: empty -> no button, set -> button.
        empty = _settings(
            BOT_IVAN_TOKEN="IVAN_TOK",
            BOT_IVAN_GROUP_ID=1,
            BOT_IVAN_WEBHOOK_SECRET="",
            ADMIN_TELEGRAM_IDS=_ADMINS,
        )
        filled = _settings(
            BOT_IVAN_TOKEN="IVAN_TOK",
            BOT_IVAN_GROUP_ID=1,
            BOT_IVAN_WEBHOOK_SECRET="abc123",
            ADMIN_TELEGRAM_IDS=_ADMINS,
        )
        empty_bot = next(b for b in empty.push_team_bots if b.name == "ivan")
        filled_bot = next(b for b in filled.push_team_bots if b.name == "ivan")
        assert bool(empty_bot.webhook_secret) is False
        assert bool(filled_bot.webhook_secret) is True


# ---------------------------------------------------------------------------
# round-44 (ADR-0027 §1/§2) — fourth push bot ``business2``
#
# Identical mechanics to ivan/alexandra/andrei. Its prod group_id is operator-
# set in ``.env`` and MUST differ from 1/2/3 (else the duplicate-group_id
# fail-fast aborts startup, §2). We therefore use group_id >= 4 for the
# positive cases and a {1,2,3} value for the duplicate case.
# ---------------------------------------------------------------------------


class TestPushTeamBotBusiness2:
    @pytest.mark.parametrize(
        ("token", "group_id", "admins", "expected"),
        [
            # ADR-0027 §2: configured iff token != "" AND group_id > 0 AND admins.
            ("B2_TOK", 7, _ADMINS, True),  # task case 1: fully configured -> in
            ("", 7, _ADMINS, False),  # task case 2: empty token -> out
            ("B2_TOK", 0, _ADMINS, False),  # task case 3: group_id == 0 -> out
            ("", 0, _ADMINS, False),  # neither -> out
            ("B2_TOK", 7, "", False),  # no admins -> whole channel off
        ],
    )
    def test_business2_configured_predicate(
        self, token: str, group_id: int, admins: str, expected: bool
    ) -> None:
        s = _settings(
            BOT_BUSINESS2_TOKEN=token,
            BOT_BUSINESS2_GROUP_ID=group_id,
            ADMIN_TELEGRAM_IDS=admins,
        )
        names = [b.name for b in s.push_team_bots]
        assert ("business2" in names) is expected

    def test_business2_fully_configured_has_expected_fields(self) -> None:
        # task case 1: business2 with non-empty token AND group_id > 0 (and
        # admins present) -> materialised with exactly those fields.
        s = _settings(
            BOT_BUSINESS2_TOKEN="B2_TOK",
            BOT_BUSINESS2_GROUP_ID=7,
            ADMIN_TELEGRAM_IDS=_ADMINS,
        )
        assert s.push_team_bots == [
            PushTeamBot(name="business2", token="B2_TOK", group_id=7, webhook_secret="")
        ]

    def test_business2_added_alongside_the_other_three(self) -> None:
        # round-44 "4 push bots": business2 coexists with ivan/alexandra/andrei
        # on a distinct group_id (4, not 1/2/3). Insertion order is preserved.
        s = _settings(
            BOT_IVAN_TOKEN="A",
            BOT_IVAN_GROUP_ID=1,
            BOT_ALEXANDRA_TOKEN="B",
            BOT_ALEXANDRA_GROUP_ID=2,
            BOT_ANDREI_TOKEN="C",
            BOT_ANDREI_GROUP_ID=3,
            BOT_BUSINESS2_TOKEN="D",
            BOT_BUSINESS2_GROUP_ID=4,
            ADMIN_TELEGRAM_IDS=_ADMINS,
        )
        assert s.push_team_bots == [
            PushTeamBot(name="ivan", token="A", group_id=1, webhook_secret=""),
            PushTeamBot(name="alexandra", token="B", group_id=2, webhook_secret=""),
            PushTeamBot(name="andrei", token="C", group_id=3, webhook_secret=""),
            PushTeamBot(name="business2", token="D", group_id=4, webhook_secret=""),
        ]

    def test_business2_only_configured_returns_just_business2(self) -> None:
        # Only business2 set, the other three unconfigured -> exactly business2.
        s = _settings(
            BOT_BUSINESS2_TOKEN="D",
            BOT_BUSINESS2_GROUP_ID=4,
            ADMIN_TELEGRAM_IDS=_ADMINS,
        )
        assert [b.name for b in s.push_team_bots] == ["business2"]

    def test_business2_no_admins_returns_empty(self) -> None:
        # task case: even fully-configured business2 is gated off with no admins.
        s = _settings(
            BOT_BUSINESS2_TOKEN="D",
            BOT_BUSINESS2_GROUP_ID=4,
            ADMIN_TELEGRAM_IDS="",
        )
        assert s.push_team_bots == []


class TestPushTeamBotBusiness2WebhookSecret:
    def test_business2_secret_carried_through(self) -> None:
        # task case 6: business2 with a set webhook_secret -> proxied into the
        # PushTeamBot record.
        s = _settings(
            BOT_BUSINESS2_TOKEN="B2_TOK",
            BOT_BUSINESS2_GROUP_ID=7,
            BOT_BUSINESS2_WEBHOOK_SECRET="cafebabecafebabecafebabecafebabe",
            ADMIN_TELEGRAM_IDS=_ADMINS,
        )
        assert s.push_team_bots == [
            PushTeamBot(
                name="business2",
                token="B2_TOK",
                group_id=7,
                webhook_secret="cafebabecafebabecafebabecafebabe",
            )
        ]

    def test_business2_stays_in_list_with_empty_secret(self) -> None:
        # task case 5: business2 with an EMPTY webhook_secret is still
        # materialised (delivery still works), webhook_secret == "" (the
        # callback button + push-webhook route just deactivate — §2/§7).
        s = _settings(
            BOT_BUSINESS2_TOKEN="B2_TOK",
            BOT_BUSINESS2_GROUP_ID=7,
            BOT_BUSINESS2_WEBHOOK_SECRET="",
            ADMIN_TELEGRAM_IDS=_ADMINS,
        )
        b2 = next(b for b in s.push_team_bots if b.name == "business2")
        assert b2.webhook_secret == ""

    def test_business2_with_button_predicate_tracks_secret(self) -> None:
        # ADR-0027 §7: dispatcher attaches the button iff bool(secret).
        empty = _settings(
            BOT_BUSINESS2_TOKEN="B2_TOK",
            BOT_BUSINESS2_GROUP_ID=7,
            BOT_BUSINESS2_WEBHOOK_SECRET="",
            ADMIN_TELEGRAM_IDS=_ADMINS,
        )
        filled = _settings(
            BOT_BUSINESS2_TOKEN="B2_TOK",
            BOT_BUSINESS2_GROUP_ID=7,
            BOT_BUSINESS2_WEBHOOK_SECRET="abc123",
            ADMIN_TELEGRAM_IDS=_ADMINS,
        )
        empty_bot = next(b for b in empty.push_team_bots if b.name == "business2")
        filled_bot = next(b for b in filled.push_team_bots if b.name == "business2")
        assert bool(empty_bot.webhook_secret) is False
        assert bool(filled_bot.webhook_secret) is True


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

    # --- round-44: business2 participates in the same duplicate check -------

    def test_business2_dupe_with_ivan_group_id_raises(self) -> None:
        # task case 4: business2 configured with the SAME group_id as a real
        # bot (ivan=1, a value from the {1,2,3} reserved set) -> fail-fast at
        # startup. The error text must name BOT_BUSINESS2 (it enumerates all
        # four configured push-bot env prefixes).
        with pytest.raises(ValidationError) as exc:
            _settings(
                BOT_IVAN_TOKEN="A",
                BOT_IVAN_GROUP_ID=1,
                BOT_BUSINESS2_TOKEN="D",
                BOT_BUSINESS2_GROUP_ID=1,  # collides with ivan (reserved 1)
                ADMIN_TELEGRAM_IDS=_ADMINS,
            )
        msg = str(exc.value)
        assert "Duplicate push-bot group_id" in msg
        assert "BOT_BUSINESS2" in msg

    def test_business2_dupe_matches_canonical_message(self) -> None:
        # Same scenario, asserting via pytest.raises match= on the documented
        # ADR-0027 §2 message (mirrors the existing duplicate test style).
        with pytest.raises(ValidationError, match="Duplicate push-bot group_id"):
            _settings(
                BOT_ANDREI_TOKEN="C",
                BOT_ANDREI_GROUP_ID=3,
                BOT_BUSINESS2_TOKEN="D",
                BOT_BUSINESS2_GROUP_ID=3,  # collides with andrei (reserved 3)
                ADMIN_TELEGRAM_IDS=_ADMINS,
            )

    def test_business2_empty_token_on_same_group_id_is_not_an_error(self) -> None:
        # business2 shares ivan's group_id but has an EMPTY token -> it is NOT
        # "configured" and does not participate in the duplicate check.
        s = _settings(
            BOT_IVAN_TOKEN="A",
            BOT_IVAN_GROUP_ID=1,
            BOT_BUSINESS2_TOKEN="",
            BOT_BUSINESS2_GROUP_ID=1,
            ADMIN_TELEGRAM_IDS=_ADMINS,
        )
        assert [b.name for b in s.push_team_bots] == ["ivan"]

    def test_business2_distinct_group_id_passes_with_others(self) -> None:
        # business2 on a group_id != 1/2/3 coexists with all three -> no error.
        s = _settings(
            BOT_IVAN_TOKEN="A",
            BOT_IVAN_GROUP_ID=1,
            BOT_ALEXANDRA_TOKEN="B",
            BOT_ALEXANDRA_GROUP_ID=2,
            BOT_ANDREI_TOKEN="C",
            BOT_ANDREI_GROUP_ID=3,
            BOT_BUSINESS2_TOKEN="D",
            BOT_BUSINESS2_GROUP_ID=4,
            ADMIN_TELEGRAM_IDS=_ADMINS,
        )
        assert len(s.push_team_bots) == 4


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
