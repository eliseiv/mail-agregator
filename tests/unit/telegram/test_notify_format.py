"""Unit tests for :mod:`backend.app.telegram.notify_format` (ADR-0022 §2.4/§2.5).

The formatter is pure: it takes labels + tag list + subject + body preview and
returns a Telegram-flavoured HTML string. We verify the documented behaviours:

- 1 tag → singular ``Тег "X"`` form.
- 2+ tags → plural ``Теги "A", "B"`` form, in caller-provided order.
- Empty tag list (round-31 / ADR-0022 §2.5: TG_NOTIFY_ALL_MESSAGES on) → the tag
  line is OMITTED entirely (no ``Тег``/``Теги``/``—`` placeholder).
- Round-34: subject line (``Тема: <b>…</b>``) and a body preview line are added,
  both optional, both HTML-escaped + length-capped. Line order is
  account / [tags] / sender / Тема / preview.
- Every user-controlled value (acc_label, from_label, tag name, subject, preview)
  is HTML-escaped so a value like ``<script>`` cannot break the markup.

The two pure helpers :func:`html_to_plain` and :func:`normalize_preview` are
covered in dedicated classes below.
"""

# ruff: noqa: RUF001 RUF002 RUF003

from __future__ import annotations

import pytest

from backend.app.telegram.notify_format import (
    PREVIEW_LEN,
    SUBJECT_MAX,
    format_notification,
    html_to_plain,
    normalize_preview,
)

pytestmark = pytest.mark.unit


def _fmt(
    *,
    acc_label: str = "me@example.com",
    from_label: str = "boss@corp.com",
    tag_names: list[str] | None = None,
    subject: str | None = None,
    body_preview: str = "",
) -> str:
    """Call ``format_notification`` with round-34 defaults (subject/preview off).

    The round-31 assertions only care about account/tag/sender, so defaulting
    ``subject=None`` + ``body_preview=""`` keeps those tests asserting exactly
    the lines they did before round-34 (no extra Тема / preview line emitted).
    """
    return format_notification(
        acc_label=acc_label,
        from_label=from_label,
        tag_names=tag_names if tag_names is not None else [],
        subject=subject,
        body_preview=body_preview,
    )


class TestTagPluralisation:
    def test_single_tag_uses_singular_form(self) -> None:
        out = _fmt(tag_names=["Работа"])
        assert "Тег «<b>Работа</b>»" in out
        # The plural template marker is absent.
        assert "Теги " not in out
        # Singular tag, no subject/preview → exactly three lines.
        assert len(out.split("\n")) == 3

    def test_two_tags_uses_plural_form(self) -> None:
        out = _fmt(tag_names=["Работа", "Срочно"])
        assert "Теги " in out
        assert "<b>Работа</b>" in out
        assert "<b>Срочно</b>" in out
        # Order from caller is preserved.
        assert out.index("Работа") < out.index("Срочно")
        # Plural tag line, no subject/preview → exactly three lines.
        assert len(out.split("\n")) == 3
        # Singular marker is NOT used for >=2 tags.
        assert "Тег «" not in out

    def test_three_tags_uses_plural_form(self) -> None:
        out = _fmt(tag_names=["A", "B", "C"])
        assert "Теги " in out
        # Comma-joined.
        assert out.count("«") == 3
        assert "<b>A</b>" in out and "<b>B</b>" in out and "<b>C</b>" in out

    def test_empty_tag_list_omits_tag_line_entirely(self) -> None:
        """Round-31 (ADR-0022 §2.5): TG_NOTIFY_ALL_MESSAGES on (default) means
        a message may legitimately arrive with NO tag. The formatter must then
        omit the tag line completely — no ``Тег``/``Теги`` label and no ``—``
        placeholder — leaving exactly two lines (account + sender) when there
        is also no subject / preview.
        """
        out = _fmt(tag_names=[])
        # No tag label at all.
        assert "Тег" not in out, f"singular/plural tag label leaked: {out!r}"
        assert "Теги" not in out, f"plural tag label leaked: {out!r}"
        # No em-dash placeholder.
        assert "—" not in out, f"em-dash placeholder leaked: {out!r}"
        # Exactly two lines: account + sender.
        lines = out.split("\n")
        assert len(lines) == 2, f"expected 2 lines, got {len(lines)}: {lines!r}"
        assert lines[0].startswith("Вы получили письмо")
        assert lines[1].startswith("Отправитель")
        # The required content is still present + escaped (bold markup intact).
        assert "<b>me@example.com</b>" in out
        assert "<b>boss@corp.com</b>" in out

    def test_empty_tag_list_still_html_escapes(self) -> None:
        """No tag line, but account / sender HTML must still be escaped."""
        out = _fmt(acc_label="<script>x</script>", from_label="<i>boss</i>", tag_names=[])
        assert "<script>" not in out
        assert "&lt;script&gt;" in out
        assert "&lt;i&gt;boss&lt;/i&gt;" in out


class TestHTMLEscaping:
    """All user-controlled strings must be passed through ``html.escape``."""

    def test_acc_label_with_html_is_escaped(self) -> None:
        out = _fmt(acc_label="me<script>alert(1)</script>@x.com", tag_names=["t"])
        # Raw script tag MUST NOT appear; the escape converts < → &lt; etc.
        assert "<script>" not in out
        assert "&lt;script&gt;" in out

    def test_from_label_with_html_is_escaped(self) -> None:
        out = _fmt(from_label="<b>Boss</b>", tag_names=["t"])
        # The <b>…</b> around our own template stays bold; the user-supplied
        # <b> is now &lt;b&gt;.
        assert "&lt;b&gt;Boss&lt;/b&gt;" in out
        # Sanity: own template's <b>…</b> is still rendered (not double-escaped).
        assert "<b>&lt;b&gt;Boss&lt;/b&gt;</b>" in out

    def test_tag_name_with_html_is_escaped(self) -> None:
        out = _fmt(tag_names=['<img src=x onerror="alert(1)">'])
        assert "<img " not in out
        assert "&lt;img " in out

    def test_ampersand_and_quotes_are_escaped(self) -> None:
        out = _fmt(acc_label="a&b@x.com", from_label='He said "hi"', tag_names=["A & B"])
        assert "&amp;" in out
        # html.escape escapes `"` to `&quot;` only when quote=True (default).
        assert "&quot;" in out

    def test_multiple_user_inputs_with_html_all_escaped_together(self) -> None:
        out = _fmt(
            acc_label="<u>a</u>",
            from_label="<i>b</i>",
            tag_names=["<s>c</s>", "<em>d</em>"],
        )
        for raw in ("<u>", "<i>", "<s>", "<em>"):
            assert raw not in out, f"raw {raw!r} leaked into output"
        for escaped in ("&lt;u&gt;", "&lt;i&gt;", "&lt;s&gt;", "&lt;em&gt;"):
            assert escaped in out


class TestPayloadShape:
    def test_output_mentions_acc_from_and_tag(self) -> None:
        out = _fmt(acc_label="me@me.com", from_label="Sender Name", tag_names=["VIP"])
        assert "me@me.com" in out
        assert "Sender Name" in out
        assert "VIP" in out

    def test_template_contains_three_lines(self) -> None:
        out = _fmt(acc_label="a@a.a", from_label="b@b.b", tag_names=["t"])
        # Three lines: "Вы получили...", "Тег ...", "Отправитель ...".
        lines = out.split("\n")
        assert len(lines) == 3
        assert lines[0].startswith("Вы получили письмо")
        assert lines[2].startswith("Отправитель")

    def test_bold_account_and_sender(self) -> None:
        out = _fmt(acc_label="acc@x.com", from_label="sender@x.com", tag_names=["t"])
        assert "<b>acc@x.com</b>" in out
        assert "<b>sender@x.com</b>" in out


# ===========================================================================
# Round-34 (ADR-0022 §2.4/§2.5): subject + body preview in format_notification.
# ===========================================================================


class TestSubjectAndPreviewLines:
    def test_subject_and_preview_present_and_in_order(self) -> None:
        """A non-blank subject + non-empty preview both render, and the line
        order is account / [tags] / sender / Тема / preview."""
        out = _fmt(
            acc_label="me@example.com",
            from_label="boss@corp.com",
            tag_names=["Работа"],
            subject="Quarterly report",
            body_preview="Please review the attached numbers before Friday.",
        )
        assert "Тема: <b>Quarterly report</b>" in out
        assert "Please review the attached numbers before Friday." in out
        lines = out.split("\n")
        # account, tag, sender, Тема, preview → five lines.
        assert len(lines) == 5, f"expected 5 lines, got {lines!r}"
        assert lines[0].startswith("Вы получили письмо")
        assert lines[1].startswith("Тег ")
        assert lines[2].startswith("Отправитель")
        assert lines[3].startswith("Тема:")
        assert lines[4] == "Please review the attached numbers before Friday."
        # Strict ordering: account < tag < sender < Тема < preview.
        assert out.index("получили") < out.index("Тег ") < out.index("Отправитель")
        assert out.index("Отправитель") < out.index("Тема:") < out.index("Please review")

    def test_subject_none_omits_subject_line(self) -> None:
        out = _fmt(subject=None, body_preview="some preview")
        assert "Тема:" not in out
        # Preview still present.
        assert "some preview" in out

    def test_subject_whitespace_only_omits_subject_line(self) -> None:
        out = _fmt(subject="   ", body_preview="some preview")
        assert "Тема:" not in out

    def test_subject_newlines_only_omits_subject_line(self) -> None:
        out = _fmt(subject="\n\t  \n", body_preview="x")
        assert "Тема:" not in out

    def test_empty_preview_omits_preview_line(self) -> None:
        out = _fmt(subject="Real subject", body_preview="")
        assert "Тема: <b>Real subject</b>" in out
        lines = out.split("\n")
        # account, sender, Тема → 3 lines (no tags, no preview).
        assert len(lines) == 3, f"expected 3 lines, got {lines!r}"
        assert lines[-1].strip() != "", "trailing empty preview line leaked"

    def test_subject_and_preview_both_absent_matches_round31_shape(self) -> None:
        """No subject + no preview + no tags → exactly the round-31 two lines."""
        out = _fmt(tag_names=[], subject=None, body_preview="")
        assert len(out.split("\n")) == 2

    def test_subject_longer_than_max_is_truncated_with_ellipsis(self) -> None:
        subject = "S" * (SUBJECT_MAX + 50)
        out = _fmt(subject=subject)
        # The rendered subject text (inside <b>…</b>) is capped at SUBJECT_MAX
        # visible chars + a single ellipsis.
        assert "…" in out
        # Count of the subject letter must equal SUBJECT_MAX exactly.
        assert out.count("S") == SUBJECT_MAX
        assert ("S" * SUBJECT_MAX + "…") in out

    def test_subject_exactly_max_is_not_truncated(self) -> None:
        subject = "S" * SUBJECT_MAX
        out = _fmt(subject=subject)
        assert "…" not in out
        assert ("S" * SUBJECT_MAX) in out

    def test_subject_html_specials_are_escaped_no_injection(self) -> None:
        out = _fmt(subject='<b>boom</b> & "q" <a href="x">link</a>')
        # No raw injected markup.
        assert "<b>boom</b>" not in out
        assert "<a href" not in out
        # Entities are escaped.
        assert "&lt;b&gt;boom&lt;/b&gt;" in out
        assert "&amp;" in out
        assert "&quot;" in out
        # Our own template bold wrapper is still present (exactly the two we emit
        # for account + sender + the subject wrapper).
        assert "Тема: <b>" in out

    def test_preview_html_specials_are_escaped_no_injection(self) -> None:
        out = _fmt(body_preview='<script>alert(1)</script> & "x" <b>y</b>')
        assert "<script>" not in out
        assert "<b>y</b>" not in out
        assert "&lt;script&gt;" in out
        assert "&amp;" in out
        assert "&quot;" in out

    def test_multiline_subject_is_collapsed_no_newlines_in_subject(self) -> None:
        """Folded RFC-2047 headers can carry newlines/tabs — the subject must be
        collapsed to a single line so it never injects extra push lines."""
        out = _fmt(
            from_label="boss@corp.com",
            subject="Line one\n\tLine two\n   Line three",
        )
        lines = out.split("\n")
        # account, sender, Тема → exactly 3 lines (subject did NOT add breaks).
        assert len(lines) == 3, f"subject introduced extra lines: {lines!r}"
        subj_line = next(line for line in lines if line.startswith("Тема:"))
        assert "\n" not in subj_line
        assert subj_line == "Тема: <b>Line one Line two Line three</b>"

    def test_subject_present_preview_present_with_no_tags(self) -> None:
        """Round-31 not broken: empty tags + subject + preview → no tag line, but
        Тема + preview present (account / sender / Тема / preview = 4 lines)."""
        out = _fmt(tag_names=[], subject="Subj", body_preview="prev")
        assert "Тег" not in out
        assert "Теги" not in out
        lines = out.split("\n")
        assert len(lines) == 4
        assert lines[0].startswith("Вы получили письмо")
        assert lines[1].startswith("Отправитель")
        assert lines[2] == "Тема: <b>Subj</b>"
        assert lines[3] == "prev"


class TestHtmlToPlain:
    """``html_to_plain``: strip ALL markup + decode entities for a teaser."""

    def test_none_returns_empty(self) -> None:
        assert html_to_plain(None) == ""

    def test_empty_returns_empty(self) -> None:
        assert html_to_plain("") == ""

    def test_strips_b_and_a_tags_keeping_text(self) -> None:
        out = html_to_plain('<b>Hello</b> <a href="http://x.com">world</a>')
        assert "<b>" not in out and "</b>" not in out
        assert "<a" not in out and "</a>" not in out
        assert "Hello" in out
        assert "world" in out

    def test_drops_style_and_script_content(self) -> None:
        out = html_to_plain(
            "<style>.x{height:20px !important;}</style>"
            "<script>alert(1)</script>"
            "<p>Visible body</p>"
        )
        # The CSS / JS payload must not leak as text.
        assert "height:20px" not in out
        assert "alert(1)" not in out
        assert "!important" not in out
        assert "Visible body" in out
        # No residual tags.
        assert "<" not in out and ">" not in out

    def test_decodes_html_entities(self) -> None:
        out = html_to_plain("Tom &amp; Jerry said it&#39;s &lt;ok&gt;")
        assert "&amp;" not in out
        assert "&#39;" not in out
        assert "Tom & Jerry" in out
        assert "it's" in out
        assert "<ok>" in out

    def test_no_residual_tags_after_reduction(self) -> None:
        out = html_to_plain("<div><p>A</p><br><span>B</span></div>")
        assert "<" not in out and ">" not in out
        assert "A" in out and "B" in out


class TestNormalizePreview:
    """``normalize_preview``: collapse whitespace, strip, cap at PREVIEW_LEN."""

    def test_empty_string_returns_empty(self) -> None:
        assert normalize_preview("") == ""

    def test_whitespace_only_returns_empty(self) -> None:
        assert normalize_preview("   \n\t  ") == ""

    def test_collapses_newlines_tabs_and_runs(self) -> None:
        assert normalize_preview("a\n\nb\t\tc   d") == "a b c d"

    def test_collapses_nbsp_and_zero_width(self) -> None:
        # \xa0 = NBSP (collapsed), ​ = zero-width space (stripped first).
        assert normalize_preview("foo\xa0\xa0bar\u200bbaz") == "foo barbaz"

    def test_strips_leading_trailing_whitespace(self) -> None:
        assert normalize_preview("   hello world   ") == "hello world"

    def test_short_text_unchanged(self) -> None:
        assert normalize_preview("short text") == "short text"

    def test_longer_than_preview_len_is_truncated_with_ellipsis(self) -> None:
        text = "x" * (PREVIEW_LEN + 30)
        out = normalize_preview(text)
        assert out.endswith("…")
        # Exactly PREVIEW_LEN visible chars + ellipsis.
        assert out.count("x") == PREVIEW_LEN
        assert out == "x" * PREVIEW_LEN + "…"

    def test_exactly_preview_len_not_truncated(self) -> None:
        text = "y" * PREVIEW_LEN
        out = normalize_preview(text)
        assert "…" not in out
        assert out == text

    def test_truncation_trims_trailing_space_before_ellipsis(self) -> None:
        # Build text so that position PREVIEW_LEN-1..PREVIEW_LEN is whitespace.
        text = "w" * (PREVIEW_LEN - 1) + "   tail words after the boundary"
        out = normalize_preview(text)
        assert out.endswith("…")
        # No space immediately before the ellipsis.
        assert not out[:-1].endswith(" ")
