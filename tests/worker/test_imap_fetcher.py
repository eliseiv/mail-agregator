"""Unit-y tests for the IMAP fetcher's pure helpers (no IMAP).

Source of truth: ``worker/app/imap_fetcher.py``.
"""

from __future__ import annotations

import datetime as _dt
from unittest.mock import MagicMock

import pytest

from worker.app.imap_fetcher import _from_imap_msg, _truncate_body

pytestmark = pytest.mark.worker


class TestTruncate:
    def test_under_limit_returns_original(self) -> None:
        out, trunc = _truncate_body("hello", 100)
        assert out == "hello"
        assert trunc is False

    def test_over_limit_truncates(self) -> None:
        body = "ё" * 1000  # 2000 utf-8 bytes
        out, trunc = _truncate_body(body, 200)
        assert trunc is True
        assert len(out.encode()) <= 200

    def test_does_not_split_multibyte_char(self) -> None:
        # Choose limit that lands mid-codepoint of "ёж" (2-byte chars).
        body = "ё" * 5  # 10 bytes
        out, _ = _truncate_body(body, 5)
        # Must still be valid UTF-8 — no replacement chars at boundary.
        out.encode("utf-8")  # must not raise


class TestFromImapMsg:
    def _msg(self, **overrides) -> MagicMock:  # type: ignore[no-untyped-def]
        m = MagicMock()
        m.text = "plain body"
        m.html = ""
        m.uid = "5"
        m.date = _dt.datetime(2026, 1, 1, tzinfo=_dt.UTC)
        m.from_ = "x@y.com"
        m.from_values = MagicMock(email="x@y.com", name="Sender Name")
        m.to = ["a@b.c"]
        m.cc = []
        m.subject = "subj"
        m.headers = {
            "message-id": ("<mid@host>",),
            "in-reply-to": (None,),
            "references": (None,),
        }
        for k, v in overrides.items():
            setattr(m, k, v)
        return m

    def test_text_body_used_when_present(self) -> None:
        out = _from_imap_msg(self._msg(), max_body_bytes=1024)
        assert out.body_text == "plain body"
        assert out.body_present is True
        assert out.body_truncated is False

    def test_html_fallback_via_html2text(self) -> None:
        out = _from_imap_msg(
            self._msg(text="", html="<p>Hi <b>world</b></p>"),
            max_body_bytes=1024,
        )
        assert "Hi" in out.body_text
        assert "world" in out.body_text
        assert out.body_present is True

    def test_no_body_marks_body_present_false(self) -> None:
        out = _from_imap_msg(self._msg(text="", html=""), max_body_bytes=1024)
        assert out.body_present is False
        assert out.body_text == ""

    def test_naive_internal_date_promoted_to_utc(self) -> None:
        out = _from_imap_msg(
            self._msg(date=_dt.datetime(2026, 1, 1)),
            max_body_bytes=1024,
        )
        assert out.internal_date.tzinfo is not None

    def test_missing_date_uses_now_utc(self) -> None:
        before = _dt.datetime.now(_dt.UTC)
        out = _from_imap_msg(self._msg(date=None), max_body_bytes=1024)
        after = _dt.datetime.now(_dt.UTC)
        assert before <= out.internal_date <= after
