"""Unit tests for round-40 (ADR-0022 §2.10): stripping the bidi formatters
LRM (U+200E) and RLM (U+200F) inside
:func:`shared.html_sanitize.strip_invisible_padding`, plus the end-to-end
``sanitize_telegram_html`` → ``collapse_blank_lines_tg`` collapse of the
marketing-mail preheader spacer ("\\xa0<LRM><RLM>" runs) they enable.

Background (ADR-0022 §2.10 round-40, Glassdoor ``id=1264``): marketing mail
builds a tall preheader spacer line by repeating ``"\\xa0<LRM><RLM>"``. LRM/RLM
are Unicode category Cf (Format) — NOT whitespace, so the round-39 collapse
class ``[^\\S\\n]`` does not match them and the spacer line is treated as
"non-blank" and never collapsed. round-40 adds U+200E/U+200F to
``_INVISIBLE_PADDING_CODEPOINTS`` so ``strip_invisible_padding`` (which runs
inside ``sanitize_telegram_html`` BEFORE the collapse) turns the spacer into a
pure ``\\xa0`` run, which the round-39 whitespace class then collapses normally.

``\\xa0`` (U+00A0 NO-BREAK SPACE) is deliberately NOT stripped — it is
whitespace and is required for the collapse to fire.

ADR-0022 §2.10 round-40 test-matrix (rows ~1539-1542):
- row 1539  → :class:`TestStripInvisiblePaddingLrmRlm`
- row 1540  → :class:`TestSanitizeCollapseGlassdoorSpacer`
- row 1542  → :class:`TestStripInvisiblePaddingInsidePre`
- codepoint-set guard → :class:`TestInvisiblePaddingCodepointSet`
"""

from __future__ import annotations

import re

import pytest

from shared.html_sanitize import (
    _INVISIBLE_PADDING_CODEPOINTS,
    collapse_blank_lines_tg,
    sanitize_telegram_html,
    strip_invisible_padding,
)

pytestmark = pytest.mark.unit

# Bidi formatters added in round-40. Encoded as \uXXXX escapes so the source
# file stays free of invisible runtime characters (ruff PLE2502/PLE2515) — the
# same convention the production module uses.
_LRM = chr(0x200E)  # LEFT-TO-RIGHT MARK
_RLM = chr(0x200F)  # RIGHT-TO-LEFT MARK
# Pre-existing zero-width members (regression: still stripped after round-40).
_ZWSP = chr(0x200B)  # ZERO WIDTH SPACE
_ZWNJ = chr(0x200C)  # ZERO WIDTH NON-JOINER
_ZWJ = chr(0x200D)  # ZERO WIDTH JOINER
_WJ = chr(0x2060)  # WORD JOINER
_BOM = chr(0xFEFF)  # ZERO WIDTH NO-BREAK SPACE / BOM
_NBSP = "\xa0"  # U+00A0 NO-BREAK SPACE — deliberately PRESERVED.

# A "tall column" = two real line breaks separated only by optional horizontal
# whitespace. Used to assert the spacer artefact is gone after collapse.
_TALL_COLUMN_RE = re.compile(r"\n[^\S\n]*\n[^\S\n]*\n")


class TestStripInvisiblePaddingLrmRlm:
    """Row 1539 — ``strip_invisible_padding`` removes LRM/RLM, keeps everything
    that is not in the codepoint set."""

    def test_lrm_is_removed(self) -> None:
        assert strip_invisible_padding(f"a{_LRM}b") == "ab"

    def test_rlm_is_removed(self) -> None:
        assert strip_invisible_padding(f"a{_RLM}b") == "ab"

    def test_both_lrm_and_rlm_removed_in_one_pass(self) -> None:
        assert strip_invisible_padding(f"x{_LRM}{_RLM}y{_RLM}{_LRM}z") == "xyz"

    def test_nbsp_is_preserved(self) -> None:
        # U+00A0 is whitespace needed for round-39 collapse — must NOT be stripped.
        assert strip_invisible_padding(f"a{_NBSP}b") == f"a{_NBSP}b"

    def test_plain_text_is_untouched(self) -> None:
        assert strip_invisible_padding("Hello, world 123") == "Hello, world 123"

    def test_emoji_is_preserved(self) -> None:
        # 🔥 (U+1F525) is not in the codepoint set — survives verbatim.
        assert strip_invisible_padding(f"hot{_LRM}🔥{_RLM}fire") == "hot🔥fire"

    def test_anchor_tag_is_preserved(self) -> None:
        src = f'before{_LRM}<a href="https://e.com/?x=1&y=2">link</a>{_RLM}after'
        assert (
            strip_invisible_padding(src) == 'before<a href="https://e.com/?x=1&y=2">link</a>after'
        )

    def test_lrm_rlm_mixed_with_nbsp_only_formatters_removed(self) -> None:
        # The Glassdoor spacer atom "\xa0<LRM><RLM>" → just "\xa0".
        assert strip_invisible_padding(f"{_NBSP}{_LRM}{_RLM}") == _NBSP

    def test_glassdoor_spacer_run_reduces_to_pure_nbsp_run(self) -> None:
        spacer = (_NBSP + _LRM + _RLM) * 30
        assert strip_invisible_padding(spacer) == _NBSP * 30
        assert _LRM not in strip_invisible_padding(spacer)
        assert _RLM not in strip_invisible_padding(spacer)

    # --- Regression: the pre-round-40 zero-width members still stripped. ---

    def test_zwsp_still_removed(self) -> None:
        assert strip_invisible_padding(f"a{_ZWSP}b") == "ab"

    def test_zwnj_still_removed(self) -> None:
        assert strip_invisible_padding(f"a{_ZWNJ}b") == "ab"

    def test_zwj_still_removed(self) -> None:
        assert strip_invisible_padding(f"a{_ZWJ}b") == "ab"

    def test_word_joiner_still_removed(self) -> None:
        assert strip_invisible_padding(f"a{_WJ}b") == "ab"

    def test_bom_still_removed(self) -> None:
        assert strip_invisible_padding(f"a{_BOM}b") == "ab"

    def test_all_invisible_members_removed_at_once(self) -> None:
        noisy = f"k{_ZWSP}{_ZWNJ}{_ZWJ}{_LRM}{_RLM}{_WJ}{_BOM}v"
        assert strip_invisible_padding(noisy) == "kv"

    # --- Falsy / idempotency. ---

    def test_empty_string_returns_empty(self) -> None:
        assert strip_invisible_padding("") == ""

    def test_idempotent(self) -> None:
        once = strip_invisible_padding(f"{_NBSP}{_LRM}{_RLM}text{_LRM}")
        assert strip_invisible_padding(once) == once


class TestInvisiblePaddingCodepointSet:
    """Row "codepoint set" — the exact contents of the round-40 set."""

    def test_codepoint_set_is_exactly_round_40_seven(self) -> None:
        assert set(_INVISIBLE_PADDING_CODEPOINTS) == {
            0x200B,
            0x200C,
            0x200D,
            0x200E,
            0x200F,
            0x2060,
            0xFEFF,
        }

    def test_lrm_rlm_present(self) -> None:
        assert 0x200E in _INVISIBLE_PADDING_CODEPOINTS
        assert 0x200F in _INVISIBLE_PADDING_CODEPOINTS

    def test_nbsp_not_in_set(self) -> None:
        # U+00A0 must stay OUT — removing it would break round-39 collapse.
        assert 0x00A0 not in _INVISIBLE_PADDING_CODEPOINTS

    def test_no_duplicate_codepoints(self) -> None:
        assert len(_INVISIBLE_PADDING_CODEPOINTS) == len(set(_INVISIBLE_PADDING_CODEPOINTS))


class TestSanitizeCollapseGlassdoorSpacer:
    """Row 1540 — full ``sanitize_telegram_html`` → ``collapse_blank_lines_tg``
    flow on a Glassdoor-style preheader-spacer body. The spacer line of
    repeated ``"\\xa0<LRM><RLM>"`` between two content blocks must vanish: after
    strip it becomes a pure ``\\xa0`` run, which the round-39 collapse eats."""

    # 30+ repetitions of the Glassdoor spacer atom as a single spacer line,
    # sandwiched between two content paragraphs with blank-line padding.
    _SPACER_LINE = (_NBSP + _LRM + _RLM) * 35
    _GLASSDOOR_HTML = (
        "<p>Welcome to Glassdoor.</p>"
        f"<p>{_SPACER_LINE}</p>"
        '<p><a href="https://www.glassdoor.com/jobs?id=1264">See jobs</a></p>'
        f"<p>{_SPACER_LINE}</p>"
        "<p>Best regards, the Glassdoor team.</p>"
    )

    def _pipeline(self, html: str) -> str:
        return collapse_blank_lines_tg(sanitize_telegram_html(html))

    def test_no_lrm_or_rlm_in_output(self) -> None:
        out = self._pipeline(self._GLASSDOOR_HTML)
        assert _LRM not in out
        assert _RLM not in out

    def test_no_long_spacer_line_remains(self) -> None:
        out = self._pipeline(self._GLASSDOOR_HTML)
        # The original 35-atom spacer line must not survive as a long run of
        # nbsp/whitespace on its own line.
        assert _NBSP * 5 not in out
        assert _TALL_COLUMN_RE.search(out) is None
        assert "\n\n\n" not in out

    def test_max_consecutive_blank_run_at_most_one(self) -> None:
        out = self._pipeline(self._GLASSDOOR_HTML)
        # Paragraphs stay separated by at most one blank line ("\n\n"); no run
        # of 3+ consecutive breaks survives.
        assert not re.search(r"\n{3,}", out)

    def test_content_and_headings_intact(self) -> None:
        out = self._pipeline(self._GLASSDOOR_HTML)
        assert "Welcome to Glassdoor." in out
        assert "Best regards, the Glassdoor team." in out

    def test_link_stays_clickable(self) -> None:
        out = self._pipeline(self._GLASSDOOR_HTML)
        # bleach entity-escapes the & in the query string.
        assert "https://www.glassdoor.com/jobs?id=1264" in out
        assert "<a href=" in out and "See jobs</a>" in out

    def test_spacer_only_between_two_plain_text_lines_collapses(self) -> None:
        # Direct collapse path: the spacer atom run between two \n breaks, fed
        # through sanitize first (no surrounding HTML chrome).
        body = f"top\n{self._SPACER_LINE}\nbottom"
        out = collapse_blank_lines_tg(sanitize_telegram_html(body))
        assert _LRM not in out and _RLM not in out
        assert "top" in out and "bottom" in out


class TestStripInvisiblePaddingInsidePre:
    """Row 1542 — LRM/RLM inside ``<pre>`` are removed (``str.translate`` runs
    over the whole sanitized string, before the ``<pre>`` split), while ``\\n``
    and ``\\xa0`` inside ``<pre>`` are preserved."""

    def test_lrm_rlm_removed_inside_pre_newlines_and_nbsp_kept(self) -> None:
        src = f"<pre>line1{_LRM}{_RLM}\n\n\nline2{_NBSP}end</pre>"
        out = sanitize_telegram_html(src)
        assert _LRM not in out
        assert _RLM not in out
        # Significant newlines inside <pre> survive (sanitize keeps them; only
        # collapse_blank_lines_tg leaves <pre> untouched too — but sanitize's
        # own \n{3,} collapse runs globally, so assert content + nbsp instead).
        assert "line1" in out
        assert "line2" in out
        assert _NBSP in out

    def test_collapse_does_not_collapse_pre_inner_newlines(self) -> None:
        # collapse_blank_lines_tg must NOT touch newlines inside <pre>. We feed
        # it a string already shaped like sanitize output (LRM/RLM stripped, one
        # blank line inside <pre>) so the only operator under test is the
        # round-39 collapse — it must leave the <pre> inner gap intact while
        # collapsing the outer 3+ break runs to "\n\n". (sanitize's own round-13
        # \n{3,} pass — which DOES run inside <pre> — is asserted separately.)
        collapsed = collapse_blank_lines_tg("a\n\n\n<pre>p\n\nq</pre>\n\n\nb")
        assert "<pre>" in collapsed and "</pre>" in collapsed
        pre_inner = collapsed.split("<pre>")[1].split("</pre>")[0]
        assert pre_inner == "p\n\nq"  # untouched by collapse
        # Outer runs collapsed to a single paragraph separator.
        assert collapsed == "a\n\n<pre>p\n\nq</pre>\n\nb"

    def test_sanitize_strips_lrm_rlm_inside_pre_keeps_a_newline_gap(self) -> None:
        # End-to-end through sanitize: LRM/RLM inside <pre> are gone, and a
        # newline gap inside <pre> survives (sanitize's round-13 \n{3,} pass
        # caps it at "\n\n", but the line break itself is preserved — not
        # flattened to one line).
        out = sanitize_telegram_html(f"<pre>p{_LRM}\n\n\n\nq{_RLM}</pre>")
        assert _LRM not in out and _RLM not in out
        assert "<pre>" in out and "</pre>" in out
        pre_inner = out.split("<pre>")[1].split("</pre>")[0]
        assert "\n" in pre_inner  # significant newline preserved
        assert pre_inner.replace("\n", "") == "pq"

    def test_nbsp_inside_pre_not_stripped(self) -> None:
        out = sanitize_telegram_html(f"<pre>x{_NBSP}{_NBSP}y</pre>")
        assert f"x{_NBSP}{_NBSP}y" in out
