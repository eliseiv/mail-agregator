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
