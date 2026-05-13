"""Unit tests for :mod:`backend.app.telegram.notify_format` (ADR-0022 §2.5).

The formatter is pure: it takes labels + tag list, returns a Telegram-flavoured
HTML string. We verify the documented behaviours:

- 1 tag → singular ``Тег "X"`` form.
- 2+ tags → plural ``Теги "A", "B"`` form, in caller-provided order.
- Every user-controlled value (acc_label, from_label, tag name) is HTML-escaped
  so a subject like ``<script>`` cannot break the markup.
- Empty tag list emits a benign placeholder (defensive — caller should never
  send this in practice).
"""

from __future__ import annotations

import pytest

from backend.app.telegram.notify_format import format_notification

pytestmark = pytest.mark.unit


class TestTagPluralisation:
    def test_single_tag_uses_singular_form(self) -> None:
        out = format_notification(
            acc_label="me@example.com",
            from_label="boss@corp.com",
            tag_names=["Работа"],
        )
        assert "Тег &laquo;<b>Работа</b>&raquo;" in out
        # The plural template marker is absent.
        assert "Теги " not in out

    def test_two_tags_uses_plural_form(self) -> None:
        out = format_notification(
            acc_label="me@example.com",
            from_label="boss@corp.com",
            tag_names=["Работа", "Срочно"],
        )
        assert "Теги " in out
        assert "<b>Работа</b>" in out
        assert "<b>Срочно</b>" in out
        # Order from caller is preserved.
        assert out.index("Работа") < out.index("Срочно")

    def test_three_tags_uses_plural_form(self) -> None:
        out = format_notification(
            acc_label="me@example.com",
            from_label="boss@corp.com",
            tag_names=["A", "B", "C"],
        )
        assert "Теги " in out
        # Comma-joined.
        assert out.count("&laquo;") == 3
        assert "<b>A</b>" in out and "<b>B</b>" in out and "<b>C</b>" in out

    def test_empty_tag_list_uses_placeholder(self) -> None:
        """Defensive: dispatcher should never call us with no tags, but if
        it does we surface a benign placeholder instead of crashing.
        """
        out = format_notification(
            acc_label="me@example.com",
            from_label="boss@corp.com",
            tag_names=[],
        )
        # Per implementation: ``Тег "—"`` placeholder. We just assert the
        # output is non-empty and well-formed (no Python error or empty
        # tag block).
        assert "<b>" in out
        assert out.strip() != ""


class TestHTMLEscaping:
    """All user-controlled strings must be passed through ``html.escape``."""

    def test_acc_label_with_html_is_escaped(self) -> None:
        out = format_notification(
            acc_label="me<script>alert(1)</script>@x.com",
            from_label="b@x.com",
            tag_names=["t"],
        )
        # Raw script tag MUST NOT appear; the escape converts < → &lt; etc.
        assert "<script>" not in out
        assert "&lt;script&gt;" in out

    def test_from_label_with_html_is_escaped(self) -> None:
        out = format_notification(
            acc_label="a@x.com",
            from_label="<b>Boss</b>",
            tag_names=["t"],
        )
        # The <b>…</b> around our own template stays bold; the user-supplied
        # <b> is now &lt;b&gt;.
        assert "&lt;b&gt;Boss&lt;/b&gt;" in out
        # Sanity: own template's <b>…</b> is still rendered (not double-escaped).
        assert "<b>&lt;b&gt;Boss&lt;/b&gt;</b>" in out

    def test_tag_name_with_html_is_escaped(self) -> None:
        out = format_notification(
            acc_label="a@x.com",
            from_label="b@x.com",
            tag_names=['<img src=x onerror="alert(1)">'],
        )
        assert "<img " not in out
        assert "&lt;img " in out

    def test_ampersand_and_quotes_are_escaped(self) -> None:
        out = format_notification(
            acc_label="a&b@x.com",
            from_label='He said "hi"',
            tag_names=["A & B"],
        )
        assert "&amp;" in out
        # html.escape escapes `"` to `&quot;` only when quote=True (default).
        assert "&quot;" in out

    def test_multiple_user_inputs_with_html_all_escaped_together(self) -> None:
        out = format_notification(
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
        out = format_notification(
            acc_label="me@me.com",
            from_label="Sender Name",
            tag_names=["VIP"],
        )
        assert "me@me.com" in out
        assert "Sender Name" in out
        assert "VIP" in out

    def test_template_contains_three_lines(self) -> None:
        out = format_notification(
            acc_label="a@a.a",
            from_label="b@b.b",
            tag_names=["t"],
        )
        # Three lines: "Вы получили...", "Тег ...", "Отправитель ...".
        lines = out.split("\n")
        assert len(lines) == 3
        assert lines[0].startswith("Вы получили письмо")
        assert lines[2].startswith("Отправитель")

    def test_bold_account_and_sender(self) -> None:
        out = format_notification(
            acc_label="acc@x.com",
            from_label="sender@x.com",
            tag_names=["t"],
        )
        assert "<b>acc@x.com</b>" in out
        assert "<b>sender@x.com</b>" in out
