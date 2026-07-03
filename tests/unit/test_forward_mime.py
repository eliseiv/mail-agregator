"""Unit tests for ``build_forward_mime`` (ADR-0034 §4).

Source of truth: ``backend/app/send/mime.py``. The builder is pure/sync — it
consumes a duck-typed ``Message`` (the fields it reads) plus a resolved
``ForwardAttachmentPart`` list, so no DB / MinIO is needed here.

Covered: Subject ``Fwd: ...`` (incl. empty), From/To, fresh Message-ID,
``X-Forwarded-By`` loop-guard stamp, the "Пересланное сообщение" prefix block,
text + html alternative, html-escaping of hostile values, attachment inclusion,
and the skipped-(oversized)-attachment note.
"""

from __future__ import annotations

from datetime import UTC, datetime
from email.message import EmailMessage
from types import SimpleNamespace
from typing import Any

import pytest

from backend.app.send.mime import ForwardAttachmentPart, build_forward_mime

pytestmark = pytest.mark.unit


def _msg(
    *,
    subject: str | None = "Quarterly report",
    from_name: str | None = "Alice Sender",
    from_addr: str = "alice@partner.com",
    to_addrs: str = "team-mailbox@company.com",
    body_text: str = "Please review the attached report.",
    body_html: str | None = "<p>Please review.</p>",
) -> Any:  # duck-typed stand-in for shared.models.Message
    return SimpleNamespace(
        subject=subject,
        from_name=from_name,
        from_addr=from_addr,
        to_addrs=to_addrs,
        internal_date=datetime(2026, 7, 1, 9, 30, tzinfo=UTC),
        body_text=body_text,
        body_html=body_html,
    )


def _text_and_html(msg: EmailMessage) -> tuple[str, str | None]:
    text_part = None
    html_part = None
    for part in msg.walk():
        ct = part.get_content_type()
        if ct == "text/plain" and text_part is None:
            text_part = part.get_content()
        elif ct == "text/html" and html_part is None:
            html_part = part.get_content()
    assert text_part is not None
    return text_part, html_part


class TestHeaders:
    def test_subject_prefixed_with_fwd(self) -> None:
        msg = build_forward_mime(
            account_email="box@company.com",
            forward_to="leader@company.com",
            message=_msg(subject="Hello"),
            attachment_parts=[],
        )
        assert msg["Subject"] == "Fwd: Hello"

    def test_empty_subject_uses_placeholder(self) -> None:
        msg = build_forward_mime(
            account_email="box@company.com",
            forward_to="leader@company.com",
            message=_msg(subject=None),
            attachment_parts=[],
        )
        assert msg["Subject"] == "Fwd: (без темы)"

    def test_from_is_mailbox_to_is_leader(self) -> None:
        msg = build_forward_mime(
            account_email="box@company.com",
            forward_to="leader@company.com",
            message=_msg(),
            attachment_parts=[],
        )
        assert msg["From"] == "box@company.com"
        assert msg["To"] == "leader@company.com"

    def test_has_fresh_message_id_and_loop_guard_stamp(self) -> None:
        msg = build_forward_mime(
            account_email="box@company.com",
            forward_to="leader@company.com",
            message=_msg(),
            attachment_parts=[],
        )
        assert msg["Message-ID"]
        assert msg["Message-ID"].startswith("<")
        assert msg["X-Forwarded-By"] == "mail-aggregator"


class TestBody:
    def test_prefix_block_present_in_text(self) -> None:
        msg = build_forward_mime(
            account_email="box@company.com",
            forward_to="leader@company.com",
            message=_msg(subject="Quarterly report"),
            attachment_parts=[],
        )
        text, _ = _text_and_html(msg)
        assert "Пересланное сообщение" in text
        assert "От: Alice Sender" in text
        assert "Тема: Quarterly report" in text
        assert "Кому: team-mailbox@company.com" in text
        assert "2026-07-01" in text
        # Original body is preserved after the prefix block.
        assert "Please review the attached report." in text

    def test_html_alternative_present_when_source_has_html(self) -> None:
        msg = build_forward_mime(
            account_email="box@company.com",
            forward_to="leader@company.com",
            message=_msg(body_html="<p>Please review.</p>"),
            attachment_parts=[],
        )
        _, html = _text_and_html(msg)
        assert html is not None
        assert "Пересланное сообщение" in html
        assert "<p>Please review.</p>" in html

    def test_no_html_alternative_when_source_lacks_html(self) -> None:
        msg = build_forward_mime(
            account_email="box@company.com",
            forward_to="leader@company.com",
            message=_msg(body_html=None),
            attachment_parts=[],
        )
        _, html = _text_and_html(msg)
        assert html is None

    def test_hostile_header_values_are_html_escaped(self) -> None:
        msg = build_forward_mime(
            account_email="box@company.com",
            forward_to="leader@company.com",
            message=_msg(
                subject="<script>alert(1)</script>",
                from_name="<b>Mallory</b>",
                body_html="<p>hi</p>",
            ),
            attachment_parts=[],
        )
        _, html = _text_and_html(msg)
        assert html is not None
        # The injected markup from the *prefix block* fields must be escaped.
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
        assert "&lt;b&gt;Mallory&lt;/b&gt;" in html
        # And the raw hostile markup must NOT appear as live tags in the prefix.
        assert "<script>alert(1)</script>" not in html


class TestAttachments:
    def test_included_attachment_is_attached(self) -> None:
        part = ForwardAttachmentPart("report.pdf", "application/pdf", b"%PDF-1.4 data")
        msg = build_forward_mime(
            account_email="box@company.com",
            forward_to="leader@company.com",
            message=_msg(),
            attachment_parts=[part],
        )
        attached = [p for p in msg.walk() if p.get_filename() == "report.pdf"]
        assert len(attached) == 1
        assert attached[0].get_content_type() == "application/pdf"
        assert attached[0].get_payload(decode=True) == b"%PDF-1.4 data"

    def test_skipped_attachment_listed_in_body_not_attached(self) -> None:
        # data=None marks a skipped (oversized / over-budget) attachment.
        skipped = ForwardAttachmentPart("huge.zip", "application/zip", None)
        msg = build_forward_mime(
            account_email="box@company.com",
            forward_to="leader@company.com",
            message=_msg(body_html="<p>hi</p>"),
            attachment_parts=[skipped],
        )
        # Not attached…
        assert [p for p in msg.walk() if p.get_filename() == "huge.zip"] == []
        # …but named in both the text and html note.
        text, html = _text_and_html(msg)
        assert "huge.zip" in text
        assert html is not None
        assert "huge.zip" in html

    def test_mixed_included_and_skipped(self) -> None:
        parts = [
            ForwardAttachmentPart("ok.txt", "text/plain", b"hello"),
            ForwardAttachmentPart("big.bin", "application/octet-stream", None),
        ]
        msg = build_forward_mime(
            account_email="box@company.com",
            forward_to="leader@company.com",
            message=_msg(),
            attachment_parts=parts,
        )
        assert len([p for p in msg.walk() if p.get_filename() == "ok.txt"]) == 1
        assert [p for p in msg.walk() if p.get_filename() == "big.bin"] == []
        text, _ = _text_and_html(msg)
        assert "big.bin" in text
        assert "ok.txt" not in text.split("Вложения не пересланы")[-1]

    def test_malformed_content_type_falls_back_to_octet_stream(self) -> None:
        part = ForwardAttachmentPart("weird", "not-a-mime", b"x")
        msg = build_forward_mime(
            account_email="box@company.com",
            forward_to="leader@company.com",
            message=_msg(),
            attachment_parts=[part],
        )
        attached = [p for p in msg.walk() if p.get_filename() == "weird"]
        assert len(attached) == 1
        assert attached[0].get_content_type() == "application/octet-stream"
