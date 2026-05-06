"""Unit tests for argon2 password hashing + anti-timing parity used in login.

Source of truth: ``backend/app/auth/service.py`` (``_DUMMY_HASH``,
:meth:`AuthService.login` anti-timing branch).
"""

from __future__ import annotations

import time

import pytest
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from backend.app.auth.service import _DUMMY_HASH

pytestmark = pytest.mark.unit


@pytest.fixture
def ph() -> PasswordHasher:
    return PasswordHasher()


class TestHashAndVerify:
    def test_hash_then_verify_succeeds(self, ph: PasswordHasher) -> None:
        h = ph.hash("correct horse battery staple")
        assert ph.verify(h, "correct horse battery staple") is True

    def test_verify_wrong_password_raises(self, ph: PasswordHasher) -> None:
        h = ph.hash("right")
        with pytest.raises(VerifyMismatchError):
            ph.verify(h, "wrong")

    def test_two_hashes_of_same_password_differ(self, ph: PasswordHasher) -> None:
        # argon2 prepends a random salt — outputs must be unique.
        h1 = ph.hash("p")
        h2 = ph.hash("p")
        assert h1 != h2


class TestDummyHash:
    def test_dummy_hash_is_a_real_argon2_hash(self) -> None:
        # Should start with the argon2id family prefix.
        assert _DUMMY_HASH.startswith("$argon2")

    def test_dummy_hash_is_consistent(self) -> None:
        # Module-level constant — must be the same object on each import.
        from backend.app.auth.service import _DUMMY_HASH as second_import

        assert _DUMMY_HASH is second_import


class TestAntiTimingParity:
    """The login path runs verify() against either the user's hash or the
    dummy hash. Average wall-clock difference between known/unknown user
    must be small. Argon2 is intentionally slow — we just want the two
    paths to take the *same* time within a sane envelope.
    """

    def test_verify_known_and_unknown_take_similar_time(
        self, ph: PasswordHasher
    ) -> None:
        real_hash = ph.hash("real-password")

        # Verify real hash w/ wrong password — same code path login takes
        # for "user exists, wrong password".
        t0 = time.perf_counter()
        try:
            ph.verify(real_hash, "wrong-password")
        except VerifyMismatchError:
            pass
        t_known = time.perf_counter() - t0

        # Verify against dummy — login does the same when user is missing.
        t0 = time.perf_counter()
        try:
            ph.verify(_DUMMY_HASH, "wrong-password")
        except VerifyMismatchError:
            pass
        t_dummy = time.perf_counter() - t0

        # Allow generous tolerance — argon2 cost can vary by GC pauses, etc.
        # We expect the ratio to stay within 4x on a healthy machine.
        ratio = max(t_known, t_dummy) / max(min(t_known, t_dummy), 1e-9)
        assert ratio < 4.0, (
            f"timing parity broken: known={t_known:.4f}s "
            f"dummy={t_dummy:.4f}s ratio={ratio:.2f}"
        )


class TestCheckNeedsRehash:
    def test_check_needs_rehash_false_for_fresh_hash(self, ph: PasswordHasher) -> None:
        assert ph.check_needs_rehash(ph.hash("p")) is False
