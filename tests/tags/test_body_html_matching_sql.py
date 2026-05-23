"""round-29: ``body_contains`` matches ``body_text`` OR text from ``body_html``.

ADR-0017 §4.3 + ``backend/app/tags/sql.py`` round-29 block. A message is
stored in two bodies: ``body_text`` (the ``text/plain`` part, or
``html2text(html)`` when no plain part) and ``body_html`` (the raw
``text/html`` part). **The UI renders ``body_html``**, so that is the text
the user reads with their eyes. Apple's MIME mail carries *different* text in
the two parts, so pre-round-29 a ``body_contains`` rule matching the visible
HTML phrase never fired (it only saw ``body_text``).

The fix makes the ``body_contains`` arm match if the (whole-word,
case-sensitive, whitespace-normalised) pattern is found in **either**
``norm(body_text)`` **or** ``norm(strip_tags(COALESCE(body_html,'')))`` where
``strip_tags(x) = regexp_replace(x, '<[^>]+>', ' ', 'g')`` (each tag → a
space, applied before ``norm()`` so the spaces at tag seams collapse).

Every behaviour is verified through BOTH production queries via the shared
``_run_both`` helper (``tests/tags/test_tag_matching_sql.py``):

* ``APPLY_TAGS_TO_MESSAGE``  — worker auto-tag hook (binds :body_html).
* ``APPLY_TAG_TO_EXISTING``  — apply-to-existing bulk path (reads m.body_html).

The helper returns ``(matched_via_worker, matched_via_existing)``; both arms
must agree because they share the exact same SQL predicate.
"""

from __future__ import annotations

import pytest

from tests.tags.conftest import Seeder
from tests.tags.test_tag_matching_sql import APPLE_PHRASE, _run_both

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# The real production reject case (the bug round-29 fixes).
#
# Apple's "reject" mail: text/plain says one thing (NO trigger phrase),
# text/html says another (HAS the trigger phrase, with <br> inside it).
# ---------------------------------------------------------------------------

# text/plain — does NOT contain APPLE_PHRASE.
APPLE_BODY_TEXT_NO_PATTERN = "During our review, we noticed an issue with your submission."

# text/html — DOES contain APPLE_PHRASE, but split across <br> tags so the
# phrase only re-forms after strip_tags + norm collapses the seams.
APPLE_BODY_HTML_WITH_PATTERN = (
    "<html><body><p>Hello,</p>"
    "<p>We noticed an issue<br>with your submission that requires<br>"
    "your attention.</p>"
    "<p>Regards,<br>App Store Connect</p></body></html>"
)


# ---------------------------------------------------------------------------
# A. THE ROOT CASE — match_mode='all' (sender on display-name + body in HTML).
# ---------------------------------------------------------------------------


# The production 'all'-mode tag: display-name sender + the trigger phrase.
APPLE_RULES: list[tuple[str, str]] = [
    ("sender_contains", "App Store Connect"),
    ("body_contains", APPLE_PHRASE),
]


class TestRootAppleRejectCase:
    """The production bug: an 'all'-mode tag (sender_contains 'App Store
    Connect' + body_contains the Apple phrase) must attach to the reject mail
    whose phrase lives only in body_html (with <br> inside), while body_text
    carries a different sentence.
    """

    async def test_phrase_in_html_only_all_mode_attaches_both_paths(self, seed: Seeder) -> None:
        """APPLY_TAG_TO_EXISTING (m.body_html) AND APPLY_TAGS_TO_MESSAGE
        (bind :body_html) both attach via the HTML body, even though
        body_text lacks the phrase.
        """
        w, e = await _run_both(
            seed,
            body=APPLE_BODY_TEXT_NO_PATTERN,  # no pattern here
            body_html=APPLE_BODY_HTML_WITH_PATTERN,  # pattern here (with <br>)
            from_addr="no_reply@email.apple.com",
            from_name="App Store Connect",
            match_mode="all",
            rules=APPLE_RULES,
        )
        assert e is True, "apply-to-existing did not match the phrase via body_html"
        assert w is True, "worker hook did not match the phrase via :body_html bind"

    async def test_control_no_html_and_no_pattern_in_text_does_not_attach(
        self, seed: Seeder
    ) -> None:
        """CONTROL: same mail but body_html is NULL and the phrase is absent
        from body_text → the 'all'-mode tag must NOT attach (the body rule
        fails, dropping the whole tag).
        """
        w, e = await _run_both(
            seed,
            body=APPLE_BODY_TEXT_NO_PATTERN,
            body_html=None,
            from_addr="no_reply@email.apple.com",
            from_name="App Store Connect",
            match_mode="all",
            rules=APPLE_RULES,
        )
        assert (w, e) == (False, False)


# ---------------------------------------------------------------------------
# B. body_html NULL → html branch does not error; matches only via body_text.
# ---------------------------------------------------------------------------


class TestBodyHtmlNull:
    async def test_null_html_pattern_in_text_attaches(self, seed: Seeder) -> None:
        """COALESCE(body_html,'') keeps the html arm from erroring on NULL;
        the pattern is in body_text → attaches via the text arm.
        """
        w, e = await _run_both(
            seed,
            body="We noticed an issue with your submission that requires your attention.",
            body_html=None,
            match_mode="any",
            rules=[("body_contains", APPLE_PHRASE)],
        )
        assert (w, e) == (True, True)

    async def test_null_html_pattern_absent_from_text_does_not_attach(self, seed: Seeder) -> None:
        w, e = await _run_both(
            seed,
            body="nothing relevant in here at all",
            body_html=None,
            match_mode="any",
            rules=[("body_contains", APPLE_PHRASE)],
        )
        assert (w, e) == (False, False)


# ---------------------------------------------------------------------------
# C. strip_tags at a tag seam — pattern spans </p><p> and must re-form.
# ---------------------------------------------------------------------------


class TestStripTagsSeam:
    async def test_pattern_across_p_tag_seam_matches(self, seed: Seeder) -> None:
        """``foo</p><p>bar`` → strip_tags inserts spaces, norm() collapses the
        run, so the whole-word multi-word pattern "foo bar" matches.
        """
        w, e = await _run_both(
            seed,
            body="unrelated plain text",  # NOT in body_text
            body_html="<p>foo</p><p>bar</p>",
            match_mode="any",
            rules=[("body_contains", "foo bar")],
        )
        assert (w, e) == (True, True)

    async def test_seam_match_is_whole_word(self, seed: Seeder) -> None:
        """Whole-word guarantee survives strip_tags: pattern "foo bar" must
        NOT match HTML that only contains "xfoo barx" as a token.
        """
        w, e = await _run_both(
            seed,
            body="unrelated",
            body_html="<p>xfoo</p><p>barx</p>",
            match_mode="any",
            rules=[("body_contains", "foo bar")],
        )
        assert (w, e) == (False, False)


# ---------------------------------------------------------------------------
# D. match_mode='any' — pattern in body_html alone attaches the tag.
# ---------------------------------------------------------------------------


class TestAnyModeHtmlOnly:
    async def test_any_mode_single_rule_matches_via_html(self, seed: Seeder) -> None:
        w, e = await _run_both(
            seed,
            body="plain part without the phrase",
            body_html="<div>We noticed an issue<br>with your submission "
            "that requires<br>your attention.</div>",
            match_mode="any",
            rules=[("body_contains", APPLE_PHRASE)],
        )
        assert (w, e) == (True, True)


# ---------------------------------------------------------------------------
# E. Negative — phrase in NEITHER body_text NOR strip_tags(body_html).
# ---------------------------------------------------------------------------


class TestNegativeNeitherBody:
    async def test_phrase_in_neither_body_does_not_attach(self, seed: Seeder) -> None:
        w, e = await _run_both(
            seed,
            body="some plain text without the trigger",
            body_html="<p>some html text without the trigger either</p>",
            match_mode="any",
            rules=[("body_contains", APPLE_PHRASE)],
        )
        assert (w, e) == (False, False)

    async def test_phrase_only_inside_a_tag_attribute_is_not_matched(self, seed: Seeder) -> None:
        """strip_tags removes ``<…>`` wholesale — text that exists only inside
        an attribute (i.e. between ``<`` and ``>``) is deleted, so it cannot
        match. Guards against an accidental "match anything in the raw HTML".
        """
        w, e = await _run_both(
            seed,
            body="plain",
            # The phrase-ish words sit inside an attribute value, never as
            # rendered text. strip_tags drops the whole <a ...> tag.
            body_html='<a title="We noticed an issue">click</a>',
            match_mode="any",
            rules=[("body_contains", "We noticed an issue")],
        )
        assert (w, e) == (False, False)


# ---------------------------------------------------------------------------
# G. TD-024 — known limitation: strip_tags does NOT decode HTML entities.
#    A pattern whose phrase contains a char that appears entity-encoded in the
#    HTML (e.g. '&' → '&amp;') is MISSED on the HTML side. This is EXPECTED
#    (documented in docs/100-known-tech-debt.md / sql.py docstring), NOT a bug.
#    xfail(strict=True): if the html arm ever starts decoding entities this
#    test will XPASS and flag that TD-024 is resolved (revisit the test).
# ---------------------------------------------------------------------------


class TestTd024EntityLimitation:
    @pytest.mark.xfail(
        strict=True,
        reason=(
            "TD-024 (known, not a bug): strip_tags removes <...> tags but does "
            "NOT decode HTML entities. body_html 'AT&amp;T' is not decoded to "
            "'AT&T', so a body_contains 'AT&T' pattern is missed on the HTML "
            "arm. body_text is entity-free so it would match there; this case "
            "isolates the HTML arm (pattern absent from body_text). Tracked in "
            "docs/100-known-tech-debt.md TD-024 + sql.py round-29 docstring."
        ),
    )
    async def test_entity_in_html_not_decoded_html_arm_misses(self, seed: Seeder) -> None:
        w, e = await _run_both(
            seed,
            body="plain body without the brand",  # pattern absent from body_text
            body_html="<p>Welcome to AT&amp;T services</p>",  # entity-encoded '&'
            match_mode="any",
            rules=[("body_contains", "AT&T")],
        )
        # Under TD-024 the html arm cannot decode &amp; → '&', so neither path
        # matches. This assert is what we WANT to be true once TD-024 is fixed;
        # today it fails (xfail) — proving the limitation is still in force.
        assert (w, e) == (True, True)

    async def test_entity_free_phrase_in_html_does_match(self, seed: Seeder) -> None:
        """Positive companion to TD-024: an entity-FREE phrase in body_html
        matches normally — i.e. TD-024 is narrowly about entities, the round-29
        fix itself works (this is why the real Apple phrase, which is
        entity-free, attaches).
        """
        w, e = await _run_both(
            seed,
            body="plain body without the brand",
            body_html="<p>Welcome to Acme services today</p>",
            match_mode="any",
            rules=[("body_contains", "Acme services")],
        )
        assert (w, e) == (True, True)
