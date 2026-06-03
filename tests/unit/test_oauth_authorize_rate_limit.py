"""Unit guards for the Outlook OAuth rate-limit policy constants.

Source of truth: ``docs/04-api-contracts.md`` §4c (authorize ``30 / час per
user``; callback ``30 / min per IP``) and ``backend/app/oauth/router.py``
(``LIMIT_OAUTH_AUTHORIZE`` / ``LIMIT_OAUTH_CALLBACK``).

These assertions need no infrastructure (they only inspect the frozen
:class:`Limit` dataclasses), so they run in the plain ``tests/unit`` CI lane.
They pin the *capacity raised 10 -> 30* change and guarantee the callback
policy was left untouched (30 / min per IP).
"""

from __future__ import annotations

from backend.app.oauth.router import LIMIT_OAUTH_AUTHORIZE, LIMIT_OAUTH_CALLBACK


class TestAuthorizeLimitPolicy:
    def test_authorize_capacity_is_30(self) -> None:
        # Raised from 10 -> 30 (headroom for connecting several Outlook
        # mailboxes back-to-back) — docs/04-api-contracts.md §4c.
        assert LIMIT_OAUTH_AUTHORIZE.capacity == 30

    def test_authorize_window_is_one_hour(self) -> None:
        assert LIMIT_OAUTH_AUTHORIZE.window_seconds == 60 * 60
        assert LIMIT_OAUTH_AUTHORIZE.window_seconds == 3600

    def test_authorize_limit_name_is_stable(self) -> None:
        # The Redis key prefix (rl:oauth_authorize:<user_id>) must not drift.
        assert LIMIT_OAUTH_AUTHORIZE.name == "oauth_authorize"


class TestCallbackLimitUntouched:
    def test_callback_capacity_is_30(self) -> None:
        assert LIMIT_OAUTH_CALLBACK.capacity == 30

    def test_callback_window_is_one_minute(self) -> None:
        # 30 / min per IP — unchanged by the authorize bump.
        assert LIMIT_OAUTH_CALLBACK.window_seconds == 60

    def test_callback_limit_name_is_stable(self) -> None:
        assert LIMIT_OAUTH_CALLBACK.name == "oauth_callback"
