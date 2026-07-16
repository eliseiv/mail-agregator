"""Unit tests for :func:`shared.html_sanitize.strip_invisible_padding` — the
stripping of zero-width / bidi formatter codepoints from mail bodies.

Marketing mail builds a tall preheader spacer line by repeating
``"\\xa0<LRM><RLM>"``. LRM (U+200E) / RLM (U+200F) are Unicode category Cf
(Format) — NOT whitespace, so a whitespace-based collapse class does not match
them and such a spacer line reads as "non-blank". ``strip_invisible_padding``
removes them (via ``_INVISIBLE_PADDING_CODEPOINTS``), reducing the spacer to a
pure ``\\xa0`` run.

``\\xa0`` (U+00A0 NO-BREAK SPACE) is deliberately NOT stripped — it is
whitespace and carries the spacer's line structure.

Live consumer: ``worker/app/imap_fetcher.py:23`` (``strip_invisible_padding``
runs over the fetched body before it is stored/pushed to CRM, ADR-0043).

TD-060 note: this suite previously also drove the ``sanitize_telegram_html`` →
``collapse_blank_lines_tg`` render pipeline (ADR-0022 §2.10 rows 1540/1542).
Both helpers were removed with the Telegram subsystem (ADR-0044); the cases
covering them went with them. Every case below targets the surviving pure
helper.
"""

from __future__ import annotations

import pytest

from shared.html_sanitize import (
    _INVISIBLE_PADDING_CODEPOINTS,
    strip_invisible_padding,
)

pytestmark = pytest.mark.unit


# Bidi formatters. Encoded as ``chr()`` escapes so the source file stays free of
# invisible runtime characters (ruff PLE2502/PLE2515) — the same convention the
# production module uses.
_LRM = chr(0x200E)  # LEFT-TO-RIGHT MARK
_RLM = chr(0x200F)  # RIGHT-TO-LEFT MARK
# Pre-existing zero-width members.
_ZWSP = chr(0x200B)  # ZERO WIDTH SPACE
_ZWNJ = chr(0x200C)  # ZERO WIDTH NON-JOINER
_ZWJ = chr(0x200D)  # ZERO WIDTH JOINER
_WJ = chr(0x2060)  # WORD JOINER
_BOM = chr(0xFEFF)  # ZERO WIDTH NO-BREAK SPACE / BOM
_NBSP = "\xa0"  # U+00A0 NO-BREAK SPACE — deliberately PRESERVED.


class TestStripInvisiblePaddingLrmRlm:
    """``strip_invisible_padding`` removes LRM/RLM, keeps everything that is not
    in the codepoint set."""

    def test_lrm_is_removed(self) -> None:
        assert strip_invisible_padding(f"a{_LRM}b") == "ab"

    def test_rlm_is_removed(self) -> None:
        assert strip_invisible_padding(f"a{_RLM}b") == "ab"

    def test_both_lrm_and_rlm_removed_in_one_pass(self) -> None:
        assert strip_invisible_padding(f"x{_LRM}{_RLM}y{_RLM}{_LRM}z") == "xyz"

    def test_nbsp_is_preserved(self) -> None:
        # U+00A0 is whitespace carrying the spacer's line structure — must NOT
        # be stripped.
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
        # The marketing spacer atom "\xa0<LRM><RLM>" → just "\xa0".
        assert strip_invisible_padding(f"{_NBSP}{_LRM}{_RLM}") == _NBSP

    def test_spacer_run_reduces_to_pure_nbsp_run(self) -> None:
        spacer = (_NBSP + _LRM + _RLM) * 30
        assert strip_invisible_padding(spacer) == _NBSP * 30
        assert _LRM not in strip_invisible_padding(spacer)
        assert _RLM not in strip_invisible_padding(spacer)

    def test_newlines_are_preserved(self) -> None:
        # Line structure is significant for the stored body — only the codepoint
        # set is dropped.
        assert strip_invisible_padding(f"a{_LRM}\n\n\nb{_RLM}") == "a\n\n\nb"

    # --- Regression: the other zero-width members are still stripped. ---

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
    """The exact contents of the codepoint set."""

    def test_codepoint_set_is_exactly_the_seven_members(self) -> None:
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
        # U+00A0 must stay OUT — it is significant whitespace.
        assert 0x00A0 not in _INVISIBLE_PADDING_CODEPOINTS

    def test_no_duplicate_codepoints(self) -> None:
        assert len(_INVISIBLE_PADDING_CODEPOINTS) == len(set(_INVISIBLE_PADDING_CODEPOINTS))
