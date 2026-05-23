"""Tag-matching SQL behaviour — ADR-0017 §4.1/§4.2/§5/§7 (round-27 fix).

This is the regression suite for the root user bug: a ``body_contains``
pattern that ENDS in punctuation ("…attention.") never matched under the
old ``\\y … \\y`` word-boundary wrapping, so a ``match_mode='all'`` tag
silently failed to attach. The fix replaced ``\\y`` with explicit boundary
classes ``(^|[^[:alnum:]_])…([^[:alnum:]_]|$)`` and added whitespace
normalisation ``norm(x)`` on both sides.

Every behaviour is verified through BOTH production queries:

* ``APPLY_TAGS_TO_MESSAGE``  — worker auto-tag on a freshly-inserted message
  (binds :subject/:body/:sender/:sender_name).
* ``APPLY_TAG_TO_EXISTING``  — apply-to-existing bulk path
  (reads m.subject/m.body_text/m.from_addr/m.from_name).

To keep each case symmetric the helper :func:`_assert_match` seeds a tag +
rules for a super-admin (full reach) and a matching message, then asserts
the tag is/ isn't applied under each query.
"""

from __future__ import annotations

import pytest

from tests.tags.conftest import Seeder

pytestmark = pytest.mark.integration

# The real Apple App Store Connect phrase from the user bug report.
APPLE_BODY = (
    "Hello,\n\nWe noticed an issue with your submission that requires your "
    "attention.\n\nRegards,\nApp Store Connect"
)
APPLE_PHRASE = "We noticed an issue with your submission that requires your attention."


async def _run_both(
    seed: Seeder,
    *,
    body: str = "body",
    body_html: str | None = None,
    subject: str | None = "Subject",
    from_addr: str = "sender@x.com",
    from_name: str | None = None,
    match_mode: str = "any",
    rules: list[tuple[str, str]],
) -> tuple[bool, bool]:
    """Seed a super-admin + account + message + tag, then run both queries
    on two *separate* messages so the ON CONFLICT clause never hides a
    second-query miss. Returns ``(matched_via_worker, matched_via_existing)``.

    ``body_html`` (round-29, ADR-0017 §4.3) is threaded into both messages
    and bound through both production queries (worker hook binds :body_html;
    apply-to-existing reads m.body_html). Defaults to NULL (legacy rows).
    """
    sa = await seed.super_admin()
    acc = await seed.mail_account(user_id=sa.id, group_id=None, email="sa@x.com")

    # Message + tag for the worker path.
    m_worker = await seed.message(
        mail_account_id=acc.id,
        subject=subject,
        body_text=body,
        body_html=body_html,
        from_addr=from_addr,
        from_name=from_name,
    )
    tag = await seed.tag(user_id=sa.id, name="t", match_mode=match_mode, rules=rules)
    await seed.apply_tags_to_message(message=m_worker, mail_account_id=acc.id)
    matched_worker = tag.id in await seed.tags_on_message(m_worker.id)

    # A second identical message for the apply-to-existing path.
    m_existing = await seed.message(
        mail_account_id=acc.id,
        subject=subject,
        body_text=body,
        body_html=body_html,
        from_addr=from_addr,
        from_name=from_name,
    )
    await seed.apply_tag_to_existing(
        tag_id=tag.id, user_id=sa.id, user_group_id=None, is_super_admin=True
    )
    matched_existing = tag.id in await seed.tags_on_message(m_existing.id)

    return matched_worker, matched_existing


# ---------------------------------------------------------------------------
# A. The root regression — trailing-punctuation patterns under match_mode=all
# ---------------------------------------------------------------------------


class TestTrailingPunctuationRegression:
    async def test_body_ending_in_period_plus_sender_all_mode_attaches(self, seed: Seeder) -> None:
        """THE BUG: a 'all'-mode tag with a body_contains ending in '.' and a
        sender_contains on the display-name must attach. Pre-fix it never did.
        """
        w, e = await _run_both(
            seed,
            body=APPLE_BODY,
            from_addr="no_reply@email.apple.com",
            from_name="App Store Connect",
            match_mode="all",
            rules=[
                ("body_contains", APPLE_PHRASE),
                ("sender_contains", "App Store Connect"),
            ],
        )
        assert w is True, "worker auto-tag did not attach the trailing-'.' tag"
        assert e is True, "apply-to-existing did not attach the trailing-'.' tag"

    async def test_body_pattern_ending_in_exclamation_matches(self, seed: Seeder) -> None:
        w, e = await _run_both(
            seed,
            body="Congratulations! Your app is now available on the App Store.",
            match_mode="any",
            rules=[("body_contains", "Congratulations!")],
        )
        assert (w, e) == (True, True)

    async def test_pattern_ending_in_punctuation_at_end_of_body(self, seed: Seeder) -> None:
        """Boundary uses ``$`` — pattern's trailing '.' sits at end-of-string."""
        w, e = await _run_both(
            seed,
            body="…that requires your attention.",
            match_mode="any",
            rules=[("body_contains", APPLE_PHRASE[-len("requires your attention.") :])],
        )
        assert (w, e) == (True, True)


# ---------------------------------------------------------------------------
# A. Whitespace normalisation — norm() collapses runs + nbsp
# ---------------------------------------------------------------------------


class TestWhitespaceNormalisation:
    async def test_phrase_broken_by_newline_still_matches(self, seed: Seeder) -> None:
        body = APPLE_BODY.replace("requires your", "requires\nyour")
        w, e = await _run_both(
            seed, body=body, match_mode="any", rules=[("body_contains", APPLE_PHRASE)]
        )
        assert (w, e) == (True, True)

    async def test_phrase_broken_by_double_space_still_matches(self, seed: Seeder) -> None:
        body = APPLE_BODY.replace("your attention", "your  attention")
        w, e = await _run_both(
            seed, body=body, match_mode="any", rules=[("body_contains", APPLE_PHRASE)]
        )
        assert (w, e) == (True, True)

    async def test_phrase_broken_by_nbsp_still_matches(self, seed: Seeder) -> None:
        # U+00A0 between two words inside the phrase.
        body = APPLE_BODY.replace("an issue", "an issue")
        w, e = await _run_both(
            seed, body=body, match_mode="any", rules=[("body_contains", APPLE_PHRASE)]
        )
        assert (w, e) == (True, True)

    async def test_sender_name_with_double_space_matches(self, seed: Seeder) -> None:
        w, e = await _run_both(
            seed,
            from_addr="no_reply@email.apple.com",
            from_name="App Store  Connect",  # double space
            match_mode="any",
            rules=[("sender_contains", "App Store Connect")],
        )
        assert (w, e) == (True, True)

    async def test_nbsp_in_pattern_normalised_too(self, seed: Seeder) -> None:
        """norm() is applied to the (escaped) pattern as well — an nbsp in the
        rule pattern collapses to a single space and still matches plain text.
        """
        w, e = await _run_both(
            seed,
            body="urgent action required today",
            match_mode="any",
            rules=[("body_contains", "action required")],
        )
        assert (w, e) == (True, True)


# ---------------------------------------------------------------------------
# A. Whole-word guarantee preserved (boundary classes)
# ---------------------------------------------------------------------------


class TestWholeWordGuarantee:
    async def test_pla_does_not_match_template(self, seed: Seeder) -> None:
        w, e = await _run_both(
            seed,
            body="please read the template carefully",
            match_mode="any",
            rules=[("body_contains", "PLA")],
        )
        # case-sensitive too, but the point here is substring-in-word rejection
        assert (w, e) == (False, False)

    async def test_pla_does_not_match_explaining(self, seed: Seeder) -> None:
        w, e = await _run_both(
            seed,
            body="the email explaining PLA-like terms",  # 'explaining' must not match
            match_mode="any",
            rules=[("body_contains", "PLAINING")],
        )
        assert (w, e) == (False, False)

    async def test_dpla_matches_inside_quotes_and_parens(self, seed: Seeder) -> None:
        """``("DPLA")`` — the pattern is bounded by '(' and '"' which are both
        non-word, so the boundary classes accept it.
        """
        w, e = await _run_both(
            seed,
            body='Program Licence Agreement ("DPLA") applies here',
            match_mode="any",
            rules=[("body_contains", "DPLA")],
        )
        assert (w, e) == (True, True)

    async def test_dpla_pattern_does_not_match_substring_xdplay(self, seed: Seeder) -> None:
        w, e = await _run_both(
            seed,
            body="the word xDPLAy is one token",
            match_mode="any",
            rules=[("body_contains", "DPLA")],
        )
        assert (w, e) == (False, False)


# ---------------------------------------------------------------------------
# A. Case-sensitivity (~, not ~*)
# ---------------------------------------------------------------------------


class TestCaseSensitivity:
    async def test_lowercase_pattern_does_not_match_uppercase_text(self, seed: Seeder) -> None:
        w, e = await _run_both(
            seed,
            body="the PLA document",
            match_mode="any",
            rules=[("body_contains", "pla")],
        )
        assert (w, e) == (False, False)

    async def test_uppercase_pattern_does_not_match_lowercase_text(self, seed: Seeder) -> None:
        w, e = await _run_both(
            seed,
            body='the term "dpla" appears',
            match_mode="any",
            rules=[("body_contains", "DPLA")],
        )
        assert (w, e) == (False, False)

    async def test_exact_case_matches(self, seed: Seeder) -> None:
        w, e = await _run_both(
            seed,
            body='the term "DPLA" appears',
            match_mode="any",
            rules=[("body_contains", "DPLA")],
        )
        assert (w, e) == (True, True)


# ---------------------------------------------------------------------------
# A. match_mode = any vs all
# ---------------------------------------------------------------------------


class TestMatchMode:
    async def test_all_mode_one_rule_fails_no_attach(self, seed: Seeder) -> None:
        w, e = await _run_both(
            seed,
            body="contains the alpha keyword only",
            from_name="App Store Connect",
            match_mode="all",
            rules=[
                ("body_contains", "alpha"),  # matches
                ("body_contains", "beta"),  # does NOT match → whole tag drops
            ],
        )
        assert (w, e) == (False, False)

    async def test_all_mode_every_rule_matches_attaches(self, seed: Seeder) -> None:
        w, e = await _run_both(
            seed,
            body="contains alpha and beta keywords",
            match_mode="all",
            rules=[("body_contains", "alpha"), ("body_contains", "beta")],
        )
        assert (w, e) == (True, True)

    async def test_any_mode_single_match_attaches(self, seed: Seeder) -> None:
        w, e = await _run_both(
            seed,
            body="contains alpha only",
            match_mode="any",
            rules=[("body_contains", "alpha"), ("body_contains", "beta")],
        )
        assert (w, e) == (True, True)

    async def test_any_mode_no_rule_matches_no_attach(self, seed: Seeder) -> None:
        w, e = await _run_both(
            seed,
            body="nothing relevant here",
            match_mode="any",
            rules=[("body_contains", "alpha"), ("body_contains", "beta")],
        )
        assert (w, e) == (False, False)


# ---------------------------------------------------------------------------
# A. sender_exact unchanged (LOWER=LOWER, no boundary / norm)
# ---------------------------------------------------------------------------


class TestSenderExact:
    async def test_sender_exact_case_insensitive_full_address(self, seed: Seeder) -> None:
        w, e = await _run_both(
            seed,
            from_addr="AppStoreNotices@apple.com",
            match_mode="any",
            rules=[("sender_exact", "appstorenotices@apple.com")],
        )
        assert (w, e) == (True, True)

    async def test_sender_exact_partial_does_not_match(self, seed: Seeder) -> None:
        w, e = await _run_both(
            seed,
            from_addr="noreply@apple.com",
            match_mode="any",
            rules=[("sender_exact", "apple.com")],  # substring, not exact
        )
        assert (w, e) == (False, False)

    async def test_sender_exact_ignores_display_name(self, seed: Seeder) -> None:
        """sender_exact is email-only — display-name must not satisfy it."""
        w, e = await _run_both(
            seed,
            from_addr="no_reply@email.apple.com",
            from_name="App Store Connect",
            match_mode="any",
            rules=[("sender_exact", "App Store Connect")],
        )
        assert (w, e) == (False, False)


# ---------------------------------------------------------------------------
# A. sender_contains matches display-name (round-25) and subject_contains
# ---------------------------------------------------------------------------


class TestSenderContainsAndSubject:
    async def test_sender_contains_matches_display_name_only(self, seed: Seeder) -> None:
        w, e = await _run_both(
            seed,
            from_addr="no_reply@email.apple.com",
            from_name="App Store Connect",
            match_mode="any",
            rules=[("sender_contains", "App Store Connect")],
        )
        assert (w, e) == (True, True)

    async def test_sender_contains_matches_email_when_name_null(self, seed: Seeder) -> None:
        w, e = await _run_both(
            seed,
            from_addr="alerts@notifications.example.com",
            from_name=None,
            match_mode="any",
            rules=[("sender_contains", "notifications")],
        )
        assert (w, e) == (True, True)

    async def test_subject_contains_whole_word_trailing_punct(self, seed: Seeder) -> None:
        w, e = await _run_both(
            seed,
            subject="Action required: Verify now!",
            match_mode="any",
            rules=[("subject_contains", "now!")],
        )
        assert (w, e) == (True, True)
