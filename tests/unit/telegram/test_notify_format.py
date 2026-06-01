"""Unit tests for :mod:`backend.app.telegram.notify_format` (ADR-0022 §2.5).

The formatter is pure: it takes labels + tag list + subject + body preview and
returns a Telegram-flavoured HTML string. Round-36 reshaped the push into an
emoji-labelled card (ADR-0022 §2.5):

    🆔: <b>{display_name||email}</b>
    #️⃣: <b>{tags ", " || "Не сортировано"}</b>
    (blank separator)
    Клиент: <b>{from_name||from_addr}</b>
    Тема: <b>{subject || "(без темы)"}</b>
    (blank separator — only when a preview follows)
    {body preview ≤100 chars — omitted when the body is empty}

Documented behaviours verified here:

- Full message → 7 lines, blank-separator indices 2 and 5, line prefixes
  🆔: / #️⃣: / Клиент: / Тема:.
- No tags (``tag_names=[]``) → ``#️⃣: <b>Не сортировано</b>`` (always present).
- No subject (``None`` / blank) → ``Тема: <b>(без темы)</b>`` (always present).
- Empty body (``body_preview=""``) → NO preview line AND no trailing blank
  separator (5 lines, no trailing empty).
- Multiple tags → joined by ``", "``.
- Every user-controlled value (acc_label, from_label, tag name, subject, preview)
  is HTML-escaped so a value like ``<script>`` cannot break the markup.
- ``PREVIEW_LEN=100`` (cap + «…»), ``SUBJECT_MAX=150`` boundary.
- ``display_name`` absent → ``email``; ``from_name`` absent → ``from_addr``
  (resolution happens in the dispatcher; here the caller passes the resolved
  ``acc_label`` / ``from_label`` so this module's contract is "render the label
  it is given").

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
    """Call ``format_notification`` with round-36 defaults.

    Defaults: no tags → ``#️⃣: <b>Не сортировано</b>``; ``subject=None`` →
    ``Тема: <b>(без темы)</b>``; ``body_preview=""`` → no preview line and no
    trailing blank separator. The id / tags / Клиент / Тема lines are ALWAYS
    present (round-36), so the minimal card is 5 lines.
    """
    return format_notification(
        acc_label=acc_label,
        from_label=from_label,
        tag_names=tag_names if tag_names is not None else [],
        subject=subject,
        body_preview=body_preview,
    )


class TestTagsLine:
    """Round-36 (ADR-0022 §2.5): the ``#️⃣:`` tags line is ALWAYS present —
    tags joined by ``", "`` or the ``Не сортировано`` fallback. No singular /
    plural form and no em-dash placeholder any more.
    """

    def test_single_tag_renders_on_hash_line(self) -> None:
        out = _fmt(tag_names=["Работа"])
        assert "#️⃣: <b>Работа</b>" in out
        # Legacy singular/plural markers are gone.
        assert "Тег «" not in out
        assert "Теги " not in out

    def test_multiple_tags_joined_by_comma_space(self) -> None:
        out = _fmt(tag_names=["Работа", "Срочно"])
        assert "#️⃣: <b>Работа, Срочно</b>" in out
        # Caller order is preserved.
        assert out.index("Работа") < out.index("Срочно")

    def test_three_tags_joined_by_comma_space(self) -> None:
        out = _fmt(tag_names=["A", "B", "C"])
        assert "#️⃣: <b>A, B, C</b>" in out

    def test_empty_tag_list_uses_not_sorted_fallback(self) -> None:
        """No tags → the line is STILL present with the ``Не сортировано``
        placeholder (round-36 replaced the round-31 "omit the line" rule)."""
        out = _fmt(tag_names=[])
        assert "#️⃣: <b>Не сортировано</b>" in out
        # No em-dash placeholder from older drafts.
        assert "—" not in out, f"em-dash placeholder leaked: {out!r}"

    def test_empty_tag_list_still_html_escapes_account_and_client(self) -> None:
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


class TestCardStructure:
    """Round-36 line shape (ADR-0022 §2.5)."""

    def test_full_message_has_7_lines_with_blank_separators_at_2_and_5(self) -> None:
        """Full card: nick + tags + from + subject + preview →
        7 lines, blank-separator indices 2 and 5, prefixes 🆔:/#️⃣:/Клиент:/Тема:.
        """
        out = _fmt(
            acc_label="Admin Inbox",
            from_label="Boss Person",
            tag_names=["Работа", "Срочно"],
            subject="Quarterly report",
            body_preview="Please review the attached numbers before Friday.",
        )
        lines = out.split("\n")
        assert len(lines) == 7, f"expected 7 lines, got {lines!r}"
        assert lines[0].startswith("🆔:")
        assert lines[1].startswith("#️⃣:")
        assert lines[2] == "", f"blank separator expected at index 2, got {lines[2]!r}"
        assert lines[3].startswith("Клиент:")
        assert lines[4].startswith("Тема:")
        assert lines[5] == "", f"blank separator expected at index 5, got {lines[5]!r}"
        assert lines[6] == "Please review the attached numbers before Friday."

    def test_full_message_exact_render(self) -> None:
        out = _fmt(
            acc_label="Admin Inbox",
            from_label="Boss Person",
            tag_names=["Работа"],
            subject="Quarterly report",
            body_preview="Body teaser here.",
        )
        assert out == (
            "🆔: <b>Admin Inbox</b>\n"
            "#️⃣: <b>Работа</b>\n"
            "\n"
            "Клиент: <b>Boss Person</b>\n"
            "Тема: <b>Quarterly report</b>\n"
            "\n"
            "Body teaser here."
        )

    def test_bold_account_and_client(self) -> None:
        out = _fmt(acc_label="acc@x.com", from_label="sender@x.com", tag_names=["t"])
        assert "🆔: <b>acc@x.com</b>" in out
        assert "Клиент: <b>sender@x.com</b>" in out

    def test_minimal_card_is_5_lines_no_trailing_blank(self) -> None:
        """No tags + no subject + empty body → the 4 mandatory lines (id, tags,
        Клиент, Тема) joined by the single mandatory blank separator → 5 lines,
        and crucially NO trailing blank separator (empty body drops it)."""
        out = _fmt(tag_names=[], subject=None, body_preview="")
        lines = out.split("\n")
        assert len(lines) == 5, f"expected 5 lines, got {lines!r}"
        assert lines[0].startswith("🆔:")
        assert lines[1] == "#️⃣: <b>Не сортировано</b>"
        assert lines[2] == ""
        assert lines[3].startswith("Клиент:")
        assert lines[4] == "Тема: <b>(без темы)</b>"
        # No trailing empty line.
        assert lines[-1] != "", f"trailing blank separator leaked: {lines!r}"
        assert not out.endswith("\n")

    def test_display_name_label_passed_through(self) -> None:
        """The dispatcher resolves ``display_name or email``; this module simply
        renders the label it is given on the 🆔 line."""
        out = _fmt(acc_label="boss@corp.com")  # email fallback already resolved
        assert "🆔: <b>boss@corp.com</b>" in out

    def test_from_addr_label_passed_through(self) -> None:
        """The dispatcher resolves ``from_name or from_addr``; rendered verbatim
        on the Клиент line."""
        out = _fmt(from_label="someone@elsewhere.com")
        assert "Клиент: <b>someone@elsewhere.com</b>" in out


class TestSubjectLine:
    """Round-36: the ``Тема:`` line is ALWAYS present (empty → ``(без темы)``)."""

    def test_subject_present_renders(self) -> None:
        out = _fmt(subject="Real subject", body_preview="prev")
        assert "Тема: <b>Real subject</b>" in out

    def test_subject_none_uses_no_subject_fallback(self) -> None:
        out = _fmt(subject=None, body_preview="some preview")
        assert "Тема: <b>(без темы)</b>" in out
        assert "some preview" in out

    def test_subject_whitespace_only_uses_no_subject_fallback(self) -> None:
        out = _fmt(subject="   ", body_preview="some preview")
        assert "Тема: <b>(без темы)</b>" in out

    def test_subject_newlines_only_uses_no_subject_fallback(self) -> None:
        out = _fmt(subject="\n\t  \n", body_preview="x")
        assert "Тема: <b>(без темы)</b>" in out


class TestPreviewLine:
    """Round-36: the preview line + its preceding blank separator are emitted
    only when ``body_preview`` is non-empty."""

    def test_empty_preview_omits_preview_line_and_trailing_blank(self) -> None:
        out = _fmt(subject="Real subject", body_preview="")
        assert "Тема: <b>Real subject</b>" in out
        lines = out.split("\n")
        # id, tags, blank, Клиент, Тема → 5 lines, NO preview, NO trailing blank.
        assert len(lines) == 5, f"expected 5 lines, got {lines!r}"
        assert lines[-1].strip() != "", "trailing empty preview separator leaked"
        assert not out.endswith("\n")

    def test_non_empty_preview_adds_blank_separator_then_preview(self) -> None:
        out = _fmt(subject="S", body_preview="The teaser.")
        lines = out.split("\n")
        assert lines[-2] == "", "preview must be preceded by a blank separator"
        assert lines[-1] == "The teaser."

    def test_preview_present_with_no_tags(self) -> None:
        """Empty tags + subject + preview → tags fallback + Тема + preview all
        present (id / tags / blank / Клиент / Тема / blank / preview = 7 lines)."""
        out = _fmt(tag_names=[], subject="Subj", body_preview="prev")
        lines = out.split("\n")
        assert len(lines) == 7
        assert lines[1] == "#️⃣: <b>Не сортировано</b>"
        assert lines[4] == "Тема: <b>Subj</b>"
        assert lines[6] == "prev"


class TestSubjectTruncation:
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
        collapsed to a single line so it never injects extra push lines.

        Round-36: the card always has the 4 mandatory lines + 1 blank separator
        (no preview here) → exactly 5 lines. The point of the test is that the
        subject itself introduces NO additional line breaks.
        """
        out = _fmt(
            from_label="boss@corp.com",
            subject="Line one\n\tLine two\n   Line three",
        )
        lines = out.split("\n")
        # id, tags, blank, Клиент, Тема → exactly 5 lines (subject did NOT break).
        assert len(lines) == 5, f"subject introduced extra lines: {lines!r}"
        subj_line = next(line for line in lines if line.startswith("Тема:"))
        assert "\n" not in subj_line
        assert subj_line == "Тема: <b>Line one Line two Line three</b>"


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
