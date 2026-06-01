"""Unit tests for the render-time blank-line collapse helpers (ADR-0022 §2.10,
round-37): :func:`shared.html_sanitize.collapse_blank_lines_text` /
:func:`collapse_blank_lines_html`.

These run at *display* time only — the STORED body is never modified. The
helpers turn the tall column of blank lines that Apple / marketing mail leaves
behind into single-line paragraph separators:

- text: runs of 3+ newlines (one or more blank lines, possibly whitespace-only)
  → a single ``\\n\\n``; a SINGLE blank line (``a\\n\\nb``) is PRESERVED;
  leading/trailing newlines stripped; ``None`` / ``""`` → ``""``.
- html: empty ``<p>``/``<div>`` separators (whitespace / ``&nbsp;`` / ``<br>``)
  removed; runs of 3+ ``<br>`` → ``<br><br>``; 2 ``<br>`` preserved; ``<pre>``
  left untouched; ``None`` / ``""`` → ``""``.
"""

from __future__ import annotations

import pytest

from shared.html_sanitize import (
    collapse_blank_lines_html,
    collapse_blank_lines_text,
)

pytestmark = pytest.mark.unit


class TestCollapseBlankLinesText:
    def test_long_run_collapses_to_single_blank_line(self) -> None:
        assert collapse_blank_lines_text("a\n\n\n\n\nb") == "a\n\nb"

    def test_single_blank_line_is_preserved(self) -> None:
        # Exactly one blank line between paragraphs must survive untouched
        # (the rule only fires on 3+ consecutive line breaks).
        assert collapse_blank_lines_text("a\n\nb") == "a\n\nb"

    def test_whitespace_only_blank_lines_are_collapsed(self) -> None:
        # The "blank" lines carry spaces — they must still collapse to one
        # paragraph break (horizontal whitespace does not block the rule).
        assert collapse_blank_lines_text("a\n  \n  \n  \nb") == "a\n\nb"

    def test_leading_and_trailing_newlines_are_stripped(self) -> None:
        assert collapse_blank_lines_text("\n\n\na\n\n\nb\n\n\n") == "a\n\nb"

    def test_none_returns_empty_string(self) -> None:
        assert collapse_blank_lines_text(None) == ""

    def test_empty_string_returns_empty_string(self) -> None:
        assert collapse_blank_lines_text("") == ""

    def test_two_separate_paragraph_gaps_each_collapse(self) -> None:
        assert collapse_blank_lines_text("a\n\n\n\nb\n\n\n\n\nc") == "a\n\nb\n\nc"

    def test_no_blank_lines_unchanged(self) -> None:
        # Single newlines (no blank line) are left exactly as-is.
        assert collapse_blank_lines_text("a\nb\nc") == "a\nb\nc"

    def test_tabs_and_carriage_returns_in_blank_lines_collapse(self) -> None:
        assert collapse_blank_lines_text("a\n\t\n\r\n \nb") == "a\n\nb"


class TestCollapseBlankLinesHtml:
    def test_empty_p_removed(self) -> None:
        out = collapse_blank_lines_html("<p>real</p><p></p><p>text</p>")
        assert "<p></p>" not in out
        assert "<p>real</p>" in out
        assert "<p>text</p>" in out

    def test_empty_p_with_nbsp_removed(self) -> None:
        out = collapse_blank_lines_html("<p>a</p><p>&nbsp;</p><p>b</p>")
        assert "&nbsp;" not in out
        assert "<p>a</p>" in out and "<p>b</p>" in out

    def test_empty_div_removed(self) -> None:
        out = collapse_blank_lines_html("<div>a</div><div></div><div>b</div>")
        assert "<div></div>" not in out
        assert "<div>a</div>" in out and "<div>b</div>" in out

    def test_div_with_only_br_removed(self) -> None:
        out = collapse_blank_lines_html("<div>a</div><div><br></div><div>b</div>")
        assert "<div><br></div>" not in out
        assert "<div>a</div>" in out and "<div>b</div>" in out

    def test_three_or_more_br_collapse_to_two(self) -> None:
        assert collapse_blank_lines_html("a<br><br><br><br>b") == "a<br><br>b"

    def test_exactly_two_br_preserved(self) -> None:
        assert collapse_blank_lines_html("a<br><br>b") == "a<br><br>b"

    def test_single_br_preserved(self) -> None:
        assert collapse_blank_lines_html("a<br>b") == "a<br>b"

    def test_pre_block_with_newlines_is_not_touched(self) -> None:
        # <pre> carries significant whitespace — collapsing would corrupt it.
        # The helper only targets empty <p>/<div> and <br> runs, so the inner
        # newlines of <pre> survive verbatim.
        src = "<pre>line1\n\n\n\nline2</pre>"
        assert collapse_blank_lines_html(src) == src

    def test_self_closing_br_variants_collapse(self) -> None:
        # <br/>, <br /> spellings are also matched by the 3+ run rule.
        assert collapse_blank_lines_html("a<br/><br /><br>b") == "a<br><br>b"

    def test_none_returns_empty_string(self) -> None:
        assert collapse_blank_lines_html(None) == ""

    def test_empty_string_returns_empty_string(self) -> None:
        assert collapse_blank_lines_html("") == ""


class TestAppleStyleArtefact:
    """The real-world case ADR-0022 §2.10 targets: 15+ blank lines between two
    short paragraphs. After collapse the content survives and the gap shrinks to
    a single-line separator."""

    def test_text_15_blank_lines_collapse_to_one_separator(self) -> None:
        body = "Hello there." + "\n" * 16 + "Best regards, the team."
        out = collapse_blank_lines_text(body)
        assert out == "Hello there.\n\nBest regards, the team."
        # Content preserved, no tall column left.
        assert "Hello there." in out
        assert "Best regards, the team." in out
        assert "\n\n\n" not in out

    def test_html_many_empty_blocks_and_br_runs_collapse(self) -> None:
        body = (
            "<p>Hello there.</p>"
            + "<p>&nbsp;</p>" * 8
            + "<div><br></div>" * 4
            + "Tail<br><br><br><br><br>more"
            + "<p>Best regards, the team.</p>"
        )
        out = collapse_blank_lines_html(body)
        # Every empty separator block is gone.
        assert "<p>&nbsp;</p>" not in out
        assert "<div><br></div>" not in out
        # The <br> run is compacted to exactly two.
        assert "<br><br><br>" not in out
        assert "Tail<br><br>more" in out
        # Real content preserved.
        assert "<p>Hello there.</p>" in out
        assert "<p>Best regards, the team.</p>" in out
