"""Unit tests for the TG full-body post-sanitize blank-line collapse
(ADR-0022 §2.10, round-39): :func:`shared.html_sanitize.collapse_blank_lines_tg`
and its wiring into ``callback_handler._format_message_body``.

``collapse_blank_lines_tg`` runs at *display* time only, on the output of
:func:`sanitize_telegram_html` (a mix of literal ``\\n`` and ``<br>``). A run of
3+ line breaks — any combination of ``\\n`` / ``<br>`` with arbitrary horizontal
whitespace between them (incl. ``\\xa0`` / em space / ideographic space) —
collapses to exactly one paragraph separator (``\\n\\n``). A single break and a
single blank line are left untouched. ``<pre>`` content is preserved verbatim.
Leading / trailing newlines are stripped. ``None`` / ``""`` → ``""``.

The integration block exercises the real ``sanitize_telegram_html`` →
``collapse_blank_lines_tg`` pipeline through ``_format_message_body`` on an
Apple-/marketing-style nested-table sample, and confirms the ``body_text`` (no
HTML) branch keeps the pre-round-39 behaviour.
"""

from __future__ import annotations

import re

import pytest

from backend.app.telegram.callback_handler import _format_message_body
from shared.html_sanitize import collapse_blank_lines_tg, sanitize_telegram_html

pytestmark = pytest.mark.unit


# A "tall column" = two real line breaks separated only by optional horizontal
# whitespace. Used to assert the artefact is gone after collapse.
_TALL_COLUMN_RE = re.compile(r"\n[^\S\n]*\n[^\S\n]*\n")


class TestCollapseBlankLinesTgBasic:
    """Case 1 — plain ``\\n`` runs. Only 3+ breaks collapse; 1 and 2 survive."""

    def test_long_newline_run_collapses_to_single_blank_line(self) -> None:
        assert collapse_blank_lines_tg("a\n\n\n\n\nb") == "a\n\nb"

    def test_single_newline_is_preserved(self) -> None:
        assert collapse_blank_lines_tg("a\nb") == "a\nb"

    def test_single_blank_line_is_preserved(self) -> None:
        # Exactly one blank line (two breaks) must survive untouched — the rule
        # only fires on 3+ consecutive breaks.
        assert collapse_blank_lines_tg("a\n\nb") == "a\n\nb"

    def test_two_separate_gaps_each_collapse(self) -> None:
        assert collapse_blank_lines_tg("a\n\n\n\nb\n\n\n\n\nc") == "a\n\nb\n\nc"


class TestCollapseBlankLinesTgHorizontalWhitespace:
    """Case 2 — wide horizontal-whitespace class between breaks still collapses."""

    def test_ascii_spaces_between_breaks_collapse(self) -> None:
        assert collapse_blank_lines_tg("a\n   \n   \n   \nb") == "a\n\nb"

    def test_nbsp_between_breaks_collapses(self) -> None:
        # U+00A0 NO-BREAK SPACE — the narrow round-37 class would miss it.
        assert collapse_blank_lines_tg("a\n\xa0\n\xa0\n\xa0\nb") == "a\n\nb"

    def test_em_space_between_breaks_collapses(self) -> None:
        # U+2003 EM SPACE.
        assert collapse_blank_lines_tg("a\n \n \n \nb") == "a\n\nb"

    def test_ideographic_space_between_breaks_collapses(self) -> None:
        # U+3000 IDEOGRAPHIC SPACE.
        assert collapse_blank_lines_tg("a\n　\n　\n　\nb") == "a\n\nb"

    def test_mixed_wide_whitespace_collapses(self) -> None:
        assert collapse_blank_lines_tg("a\n \xa0\n  \n　\nb") == "a\n\nb"


class TestCollapseBlankLinesTgMixedBreaks:
    """Case 3 — ``\\n`` and ``<br>`` are interchangeable "breaks"."""

    def test_three_br_collapse_to_paragraph_break(self) -> None:
        assert collapse_blank_lines_tg("a<br><br><br>b") == "a\n\nb"

    def test_mixed_newline_and_br_collapse(self) -> None:
        assert collapse_blank_lines_tg("a\n<br>\n<br>\nb") == "a\n\nb"

    def test_self_closing_br_variants_collapse(self) -> None:
        assert collapse_blank_lines_tg("a<br/><br /><br>b") == "a\n\nb"

    def test_single_br_not_collapsed_to_paragraph_separator(self) -> None:
        # A lone <br> is a single break, not a 3+ run — left as-is (it is NOT
        # turned into a "\n\n" paragraph separator).
        out = collapse_blank_lines_tg("a<br>b")
        assert out == "a<br>b"
        assert "\n\n" not in out

    def test_two_breaks_mixed_preserved(self) -> None:
        # Exactly two breaks (one blank line) survive even when mixed.
        assert collapse_blank_lines_tg("a\n<br>b") == "a\n<br>b"


class TestCollapseBlankLinesTgPreservesPre:
    """Case 4 (CRITICAL) — newlines INSIDE ``<pre>`` are preserved verbatim;
    collapse applies only to the text OUTSIDE ``<pre>`` segments."""

    def test_pre_inner_newlines_preserved_outer_collapsed(self) -> None:
        src = "x\n\n\n\n<pre>p\n\n\n\nq</pre>\n\n\n\ny"
        assert collapse_blank_lines_tg(src) == "x\n\n<pre>p\n\n\n\nq</pre>\n\ny"

    def test_multiple_pre_blocks_each_preserved(self) -> None:
        src = "a\n\n\n<pre>1\n\n\n2</pre>b\n\n\n<pre>3\n\n\n4</pre>c\n\n\nd"
        expected = "a\n\n<pre>1\n\n\n2</pre>b\n\n<pre>3\n\n\n4</pre>c\n\nd"
        assert collapse_blank_lines_tg(src) == expected

    def test_pre_with_attributes_preserved(self) -> None:
        src = 'a\n\n\n<pre class="language-py">k\n\n\nl</pre>'
        assert collapse_blank_lines_tg(src) == 'a\n\n<pre class="language-py">k\n\n\nl</pre>'

    def test_pre_tag_is_case_insensitive(self) -> None:
        src = "a\n\n\n<PRE>k\n\n\nl</PRE>"
        assert collapse_blank_lines_tg(src) == "a\n\n<PRE>k\n\n\nl</PRE>"


class TestCollapseBlankLinesTgPreservesAnchors:
    """Case 5 — an ``<a href>`` sitting inside / next to a break run survives
    intact and clickable (the tag is never split)."""

    def test_anchor_after_break_run_intact(self) -> None:
        out = collapse_blank_lines_tg('a<br><br><br><a href="http://e.com/?x=1&y=2">t</a>')
        assert out == 'a\n\n<a href="http://e.com/?x=1&y=2">t</a>'
        assert '<a href="http://e.com/?x=1&y=2">t</a>' in out

    def test_anchor_between_two_break_runs_intact(self) -> None:
        src = 'p\n\n\n<a href="https://x.io/a?b=1&c=2">link</a>\n\n\nq'
        out = collapse_blank_lines_tg(src)
        assert out == 'p\n\n<a href="https://x.io/a?b=1&c=2">link</a>\n\nq'
        assert '<a href="https://x.io/a?b=1&c=2">link</a>' in out


class TestCollapseBlankLinesTgEdgeCases:
    """Cases 6 & 7 — falsy input, break-only input, leading/trailing strip."""

    def test_none_returns_empty_string(self) -> None:
        assert collapse_blank_lines_tg(None) == ""

    def test_empty_string_returns_empty_string(self) -> None:
        assert collapse_blank_lines_tg("") == ""

    def test_only_breaks_returns_empty_string(self) -> None:
        assert collapse_blank_lines_tg("\n\n\n\n") == ""
        assert collapse_blank_lines_tg("<br><br><br>") == ""

    def test_leading_and_trailing_newlines_stripped(self) -> None:
        assert collapse_blank_lines_tg("\n\n\na\n\n\nb\n\n\n") == "a\n\nb"

    def test_leading_trailing_breaks_with_whitespace_stripped(self) -> None:
        assert collapse_blank_lines_tg("\xa0\n\n\na\n\n\nb") == "a\n\nb"


# Apple-/marketing-style nested-table HTML. After ``sanitize_telegram_html``
# the spacer cells (``<td>`` with only ``<br>`` + horizontal whitespace) leave a
# tall column of "newline, space, newline" runs — a pattern the round-37 narrow
# ``\\n{3,}`` collapse inside ``sanitize_telegram_html`` can NOT remove (the
# spaces break the run). ``collapse_blank_lines_tg`` is what clears it.
_APPLE_BODY_HTML = (
    "<table><tr><td>"
    "<table><tr><td>Hello there.</td></tr></table>"
    "</td></tr>"
    "<tr><td> <br> <br> <br> <br> </td></tr>"
    '<tr><td><a href="https://shop.example.com/p?id=42&utm=mail">Open the offer</a>'
    "</td></tr>"
    "<tr><td> <br> <br> <br> </td></tr>"
    "<tr><td>Best regards, the team.</td></tr></table>"
)


class TestFormatMessageBodyIntegration:
    """Case 8 — the real ``sanitize_telegram_html`` → ``collapse_blank_lines_tg``
    pipeline through ``_format_message_body``."""

    def test_sanitize_leaves_tall_column_that_collapse_removes(self) -> None:
        # Guards the premise: sanitize alone does NOT clear the artefact (its
        # narrow \n{3,} regex is defeated by the interleaved spaces); the round-39
        # helper is what removes it.
        sanitized = sanitize_telegram_html(_APPLE_BODY_HTML)
        assert _TALL_COLUMN_RE.search(sanitized) is not None
        collapsed = collapse_blank_lines_tg(sanitized)
        assert _TALL_COLUMN_RE.search(collapsed) is None

    def test_html_body_no_blank_line_column(self) -> None:
        out = _format_message_body(
            subject="Spring sale",
            from_label="shop@example.com",
            body_text="",
            body_html=_APPLE_BODY_HTML,
        )
        # No tall blank-line column survives.
        assert _TALL_COLUMN_RE.search(out) is None
        assert "\n\n\n" not in out

    def test_html_body_headers_intact(self) -> None:
        out = _format_message_body(
            subject="Spring sale",
            from_label="shop@example.com",
            body_text="",
            body_html=_APPLE_BODY_HTML,
        )
        assert "<b>Тема:</b> Spring sale" in out
        assert "<b>От:</b> shop@example.com" in out
        # Body content survives the collapse.
        assert "Hello there." in out
        assert "Best regards, the team." in out

    def test_html_body_link_stays_clickable(self) -> None:
        out = _format_message_body(
            subject="Spring sale",
            from_label="shop@example.com",
            body_text="",
            body_html=_APPLE_BODY_HTML,
        )
        # The anchor survives sanitize + collapse with its (entity-escaped) href.
        assert '<a href="https://shop.example.com/p?id=42&amp;utm=mail">Open the offer</a>' in out

    def test_subject_missing_uses_placeholder(self) -> None:
        out = _format_message_body(
            subject=None,
            from_label="shop@example.com",
            body_text="",
            body_html=_APPLE_BODY_HTML,
        )
        assert "<b>Тема:</b> <em>(без темы)</em>" in out

    def test_plain_text_body_path_unchanged(self) -> None:
        # No body_html → the legacy linkify_plain_text(strip_invisible_padding)
        # branch must be used unchanged (round-39 must not touch it).
        out = _format_message_body(
            subject="Hi",
            from_label="x@y.z",
            body_text="visit http://z.io now",
            body_html=None,
        )
        assert "<b>Тема:</b> Hi" in out
        assert "<b>От:</b> x@y.z" in out
        # linkify wraps the bare URL with rel="nofollow" (bleach.linkify default).
        assert '<a href="http://z.io" rel="nofollow">http://z.io</a>' in out

    def test_empty_html_and_text_uses_empty_body_placeholder(self) -> None:
        out = _format_message_body(
            subject="Hi",
            from_label="x@y.z",
            body_text="",
            body_html="",
        )
        assert "<em>(пустое тело)</em>" in out


# ---------------------------------------------------------------------------
# round-41 (ADR-0022 §2.10): per-line trim of leading/trailing horizontal
# whitespace OUTSIDE <pre>. Source of truth: ADR-0022 §2.10 round-41
# test-matrix (14 cases). Trim runs BEFORE the round-39 collapse; inner
# whitespace is preserved; <pre> content is verbatim.
# ---------------------------------------------------------------------------

# Detects a content line that still carries 4+ leading horizontal-whitespace
# characters after collapse — the App Store Connect (id=1267) artefact the
# round-41 trim must remove. ``(?m)`` so ``^`` matches every line start.
_LEADING_INDENT_RE = re.compile(r"(?m)^[^\S\n]{4,}\S")


class TestCollapseBlankLinesTgTrimLeading:
    """round-41 matrix #1, #4 — leading horizontal-whitespace of a content
    line is removed (ASCII spaces and Unicode ``\\xa0``)."""

    def test_leading_ascii_indent_removed(self) -> None:
        # Matrix #1: the value's leading indent (table-cell whitespace that
        # survives <td> stripping) is trimmed; the line break is preserved.
        src = "Submitted:\n                                            May 29, 2026"
        assert collapse_blank_lines_tg(src) == "Submitted:\nMay 29, 2026"

    def test_leading_indent_short_form_removed(self) -> None:
        assert collapse_blank_lines_tg("Submitted:\n                    May 29") == (
            "Submitted:\nMay 29"
        )

    def test_leading_nbsp_run_removed(self) -> None:
        # Matrix #4: U+00A0 NO-BREAK SPACE is in the [^\S\n] class.
        assert collapse_blank_lines_tg("\xa0\xa0\xa0Value") == "Value"

    def test_leading_indent_on_first_line_of_segment_removed(self) -> None:
        # \A anchor of _TG_TRIM_LEADING_RE: indent at the very segment start.
        assert collapse_blank_lines_tg("    First line") == "First line"


class TestCollapseBlankLinesTgTrimTrailing:
    """round-41 matrix #2 — trailing horizontal-whitespace of a content line
    is removed, both before a break and at segment end."""

    def test_trailing_before_break_removed(self) -> None:
        assert collapse_blank_lines_tg("value   \nnext") == "value\nnext"

    def test_trailing_at_segment_end_removed(self) -> None:
        # \Z anchor of _TG_TRIM_TRAILING_RE.
        assert collapse_blank_lines_tg("value   ") == "value"

    def test_trailing_and_leading_on_both_lines_removed(self) -> None:
        # Matrix #2: "App Name:   \n   Value   " -> "App Name:\nValue".
        assert collapse_blank_lines_tg("App Name:   \n   Value   ") == "App Name:\nValue"


class TestCollapseBlankLinesTgPreservesInnerWhitespace:
    """round-41 matrix #3 — INNER (mid-line) whitespace runs are PRESERVED;
    trim only touches the edges of each line."""

    def test_inner_double_space_preserved(self) -> None:
        # Matrix #3: the double space inside the value must survive.
        assert collapse_blank_lines_tg("May 29,  2026 at 06:10") == "May 29,  2026 at 06:10"

    def test_inner_double_space_with_edges_trimmed(self) -> None:
        # Edges trimmed, inner double-space kept simultaneously.
        assert collapse_blank_lines_tg("   May 29,  2026   ") == "May 29,  2026"

    def test_href_with_inner_space_untouched(self) -> None:
        # collapse_blank_lines_tg runs AFTER bleach (no bleach here) and only
        # trims whitespace adjacent to \n boundaries — a space INSIDE a
        # single-line href (no \n) is never touched.
        src = '<a href="http://x?a=1 b=2">t</a>'
        assert collapse_blank_lines_tg(src) == src


class TestCollapseBlankLinesTgTrimPreservesPre:
    """round-41 matrix #7 (CRITICAL) — leading/trailing whitespace of lines
    INSIDE <pre> is preserved verbatim; only outside-<pre> lines are trimmed."""

    def test_pre_indentation_preserved_verbatim(self) -> None:
        # Matrix #7: <pre> indentation is significant (code/preformatted).
        src = "<pre>\n    indented code\n        deeper\n</pre>"
        assert collapse_blank_lines_tg(src) == src

    def test_outside_pre_trimmed_inside_pre_kept(self) -> None:
        # CRITICAL task case: outer "    y" indent removed, inner <pre>
        # indentation ("    indented\n        deeper") kept verbatim.
        src = "x\n<pre>    indented\n        deeper</pre>\n    y"
        assert collapse_blank_lines_tg(src) == "x\n<pre>    indented\n        deeper</pre>\ny"

    def test_multiple_pre_blocks_inner_indent_kept_outer_trimmed(self) -> None:
        src = "  a\n<pre>  c1\n    c2</pre>  b\n<pre>  c3</pre>  d"
        expected = "a\n<pre>  c1\n    c2</pre>b\n<pre>  c3</pre>d"
        assert collapse_blank_lines_tg(src) == expected

    def test_pre_with_class_attribute_inner_indent_kept(self) -> None:
        src = '   a\n<pre class="language-py">    code\n        more</pre>   b'
        expected = 'a\n<pre class="language-py">    code\n        more</pre>b'
        assert collapse_blank_lines_tg(src) == expected

    def test_pre_uppercase_tag_inner_indent_kept(self) -> None:
        src = "   a\n<PRE>    code\n        more</PRE>   b"
        assert collapse_blank_lines_tg(src) == "a\n<PRE>    code\n        more</PRE>b"


class TestCollapseBlankLinesTgTrimMultilineAnchor:
    """round-41 matrix #6 — a multi-line ``<a href>`` keeps its href (incl.
    ``&`` entity) and the open/close tag pair intact; only the per-line edge
    whitespace is trimmed."""

    def test_multiline_anchor_edges_trimmed_href_intact(self) -> None:
        # Matrix #6 exact case.
        src = (
            '<a href="https://apps.apple.com/x">\n'
            "                https://apps.apple.com/x\n"
            "                </a>"
        )
        expected = '<a href="https://apps.apple.com/x">\nhttps://apps.apple.com/x\n</a>'
        assert collapse_blank_lines_tg(src) == expected

    def test_multiline_anchor_with_ampersand_query_preserved(self) -> None:
        # href carrying a literal & (entity-style query) must survive whole.
        src = '<a href="http://x?a=1&b=2">\n            text\n        </a>'
        out = collapse_blank_lines_tg(src)
        assert out == '<a href="http://x?a=1&b=2">\ntext\n</a>'
        assert '<a href="http://x?a=1&b=2">' in out
        assert "</a>" in out


class TestCollapseBlankLinesTgTrimOrderAndIdempotency:
    """round-41 matrix #5, #8 — trim BEFORE collapse turns whitespace-only
    lines into '' that the run then collapses; the function is idempotent."""

    def test_whitespace_only_line_becomes_empty_via_collapse(self) -> None:
        # Matrix #5/#8: "text\n\n\n\n   \n   value" -> "text\n\nvalue".
        assert collapse_blank_lines_tg("text\n\n\n\n   \n   value") == "text\n\nvalue"

    def test_whitespace_only_single_segment_returns_empty(self) -> None:
        # Matrix #5: a segment that is ONLY spaces collapses to ''.
        assert collapse_blank_lines_tg("                ") == ""

    def test_nbsp_only_single_segment_returns_empty(self) -> None:
        assert collapse_blank_lines_tg("\xa0\xa0\xa0\xa0") == ""

    def test_idempotent_second_pass_equals_first(self) -> None:
        src = "text\n\n\n\n   \n   value"
        first = collapse_blank_lines_tg(src)
        second = collapse_blank_lines_tg(first)
        assert first == second == "text\n\nvalue"

    def test_idempotent_on_trimmed_anchor_sample(self) -> None:
        src = "   App Name:   \n        \n   My App   \n\n\n   Version:   \n   1.0   "
        first = collapse_blank_lines_tg(src)
        second = collapse_blank_lines_tg(first)
        assert first == second


class TestCollapseBlankLinesTgTrimNoOpAndContent:
    """round-41 matrix #9, #11 — emoji/content untouched, plain lines no-op,
    newlines never eaten."""

    def test_emoji_preserved_leading_indent_removed(self) -> None:
        # Matrix #9: emoji are non-whitespace; only the edge indent is removed.
        assert collapse_blank_lines_tg("🔥 Promo\n   🎉 Deal") == "🔥 Promo\n🎉 Deal"

    def test_plain_line_without_edges_is_noop(self) -> None:
        # Matrix #11.
        assert collapse_blank_lines_tg("plain line") == "plain line"

    def test_plain_multiline_without_edges_is_noop(self) -> None:
        # No edge whitespace, single breaks -> nothing changes (newlines kept).
        assert collapse_blank_lines_tg("Submitted:\nMay 29\nValue") == "Submitted:\nMay 29\nValue"

    def test_single_newline_between_trimmed_lines_preserved(self) -> None:
        # The \n itself is never consumed by trim — only surrounding h-ws.
        out = collapse_blank_lines_tg("a   \n   b")
        assert out == "a\nb"
        assert out.count("\n") == 1


# Full App Store Connect (id=1267) style sample: transactional table layout
# whose <td> cells carry deep leading indentation around field values, plus a
# spacer row. After sanitize_telegram_html strips the table tags the indentation
# survives as leading whitespace of content lines — exactly the round-41 target.
_APP_STORE_CONNECT_HTML = (
    "<table><tr><td>"
    "<table>"
    "<tr><td>\n                                            Submitted:</td>"
    "<td>\n                                            May 29, 2026 at 06:10</td></tr>"
    "<tr><td>\n                                            App Name:</td>"
    "<td>\n                                            My Great App</td></tr>"
    "<tr><td> <br> <br> <br> <br> </td></tr>"
    "<tr><td>\n                                            Status:</td>"
    "<td>\n                                            Ready for Review</td></tr>"
    "</table></td></tr>"
    '<tr><td><a href="https://appstoreconnect.apple.com/apps/1267?x=1&y=2">'
    "\n                                            Open App Store Connect\n"
    "                                        </a></td></tr>"
    "</table>"
)


class TestAppStoreConnectIntegration:
    """round-41 matrix #12 — full sanitize_telegram_html -> collapse_blank_lines_tg
    pipeline on an App Store Connect-like body: no line retains a 4+ leading
    whitespace indent, no blank-line column survives, values stay compact."""

    def test_no_line_keeps_deep_leading_indent(self) -> None:
        out = collapse_blank_lines_tg(sanitize_telegram_html(_APP_STORE_CONNECT_HTML))
        # The pre-trim artefact (lines with 4+ leading h-whitespace) is gone.
        assert _LEADING_INDENT_RE.search(out) is None

    def test_no_tall_blank_column_survives(self) -> None:
        out = collapse_blank_lines_tg(sanitize_telegram_html(_APP_STORE_CONNECT_HTML))
        assert _TALL_COLUMN_RE.search(out) is None
        assert "\n\n\n" not in out

    def test_values_present_and_compact(self) -> None:
        out = collapse_blank_lines_tg(sanitize_telegram_html(_APP_STORE_CONNECT_HTML))
        assert "Submitted:" in out
        assert "May 29, 2026 at 06:10" in out
        assert "My Great App" in out
        assert "Ready for Review" in out
        # Inner double space inside the date value is preserved (not collapsed).
        assert "2026 at 06:10" in out

    def test_anchor_survives_with_escaped_href(self) -> None:
        out = collapse_blank_lines_tg(sanitize_telegram_html(_APP_STORE_CONNECT_HTML))
        # The link tag pair stays intact; href keeps its (entity-escaped) query.
        assert "appstoreconnect.apple.com/apps/1267" in out
        assert "Open App Store Connect" in out
        assert out.count("<a ") == out.count("</a>")

    def test_pipeline_idempotent(self) -> None:
        sanitized = sanitize_telegram_html(_APP_STORE_CONNECT_HTML)
        first = collapse_blank_lines_tg(sanitized)
        second = collapse_blank_lines_tg(first)
        assert first == second


class TestRound39And40Regression:
    """round-41 matrix #13, #14 — the round-39 blank-line collapse and the
    round-40 LRM/RLM spacer collapse still work after adding the trim step."""

    def test_round39_blank_run_still_collapses(self) -> None:
        # Matrix #13: round-39 invariant — 3+ break run -> one paragraph break.
        assert collapse_blank_lines_tg("a\n\n\n\nb") == "a\n\nb"

    def test_round39_wide_whitespace_run_still_collapses(self) -> None:
        assert collapse_blank_lines_tg("a\n\xa0\n\xa0\n\xa0\nb") == "a\n\nb"

    def test_round39_single_blank_line_still_preserved(self) -> None:
        assert collapse_blank_lines_tg("a\n\nb") == "a\n\nb"

    def test_round40_lrm_rlm_spacer_still_collapses(self) -> None:
        # Matrix #14: Glassdoor-style preheader spacer "\xa0<LRM><RLM>" -- after
        # strip_invisible_padding (inside sanitize_telegram_html) it becomes a
        # pure \xa0 whitespace run, which the round-39 collapse then removes.
        # round-41 trim additionally clears the residual edge \xa0. LRM/RLM are
        # written as \u200e / \u200f escapes so the source stays free of
        # invisible runtime characters (ruff PLE2502), matching the production
        # convention in shared/html_sanitize.py.
        lrm = "\u200e"  # LEFT-TO-RIGHT MARK
        rlm = "\u200f"  # RIGHT-TO-LEFT MARK
        spacer = "\xa0" + lrm + rlm
        spacer_html = (
            "<p>Headline</p>"
            f"<p>{spacer}{spacer}{spacer}</p>"
            f"<p>{spacer}{spacer}</p>"
            "<p>Body text here</p>"
        )
        out = collapse_blank_lines_tg(sanitize_telegram_html(spacer_html))
        # No LRM/RLM survive, no tall column, content intact.
        assert lrm not in out
        assert rlm not in out
        assert _TALL_COLUMN_RE.search(out) is None
        assert "Headline" in out
        assert "Body text here" in out

    def test_round40_strip_removes_lrm_rlm_keeps_nbsp(self) -> None:
        # Direct strip invariant the round-40 fix relies on (LRM/RLM as escapes).
        from shared.html_sanitize import strip_invisible_padding

        assert strip_invisible_padding("A\xa0\u200e\u200fB") == "A\xa0B"
