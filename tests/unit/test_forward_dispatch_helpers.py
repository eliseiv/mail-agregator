"""Unit tests for the pure helpers of the forward-dispatch path (ADR-0034 §3).

No DB / Redis / MinIO needed:

- ``_QueuePayload`` wire (de)serialisation + malformed handling
  (``backend/app/forwarding/dispatch_service.py``).
- ``_resolve_attachment_parts`` budget / skipped logic with a fake storage
  (``worker/app/forward_dispatch.py``).
- ``_as_aware`` / ``_safe_forward_error`` helpers (``worker/app/forward_dispatch``).
- ``_carries_own_forward_stamp`` enqueue-side loop-guard (``worker/app/sync_cycle``).
"""

from __future__ import annotations

import datetime as _dt
from types import SimpleNamespace
from typing import Any

import pytest

from backend.app.forwarding.dispatch_service import (
    FORWARD_DISPATCH_QUEUE_KEY,
    _QueuePayload,
)
from worker.app.forward_dispatch import (
    _as_aware,
    _resolve_attachment_parts,
    _safe_forward_error,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# _QueuePayload wire format
# ---------------------------------------------------------------------------


class TestQueuePayload:
    def test_roundtrip(self) -> None:
        p = _QueuePayload(message_id=42, source="sync")
        back = _QueuePayload.from_json(p.to_json())
        assert back == p

    def test_to_json_carries_version(self) -> None:
        import json

        data = json.loads(_QueuePayload(message_id=7, source="sync").to_json())
        assert data == {"v": 1, "message_id": 7, "source": "sync"}

    def test_queue_key_is_stable(self) -> None:
        assert FORWARD_DISPATCH_QUEUE_KEY == "forward_dispatch_queue"

    @pytest.mark.parametrize(
        "raw",
        [
            "not-json",
            "[1,2,3]",  # not a dict
            '{"source":"sync"}',  # missing message_id
            '{"message_id":"12"}',  # message_id not an int
        ],
    )
    def test_malformed_returns_none(self, raw: str) -> None:
        assert _QueuePayload.from_json(raw) is None

    def test_missing_source_defaults_to_sync(self) -> None:
        p = _QueuePayload.from_json('{"message_id":9}')
        assert p is not None
        assert p.source == "sync"


# ---------------------------------------------------------------------------
# _resolve_attachment_parts — budget + skipped handling
# ---------------------------------------------------------------------------


class _FakeStorage:
    """Minimal storage stub exposing ``get_object_stream`` as an async gen."""

    def __init__(self, blobs: dict[str, bytes], *, raise_on: str | None = None) -> None:
        self._blobs = blobs
        self._raise_on = raise_on

    async def get_object_stream(self, key: str) -> Any:
        if self._raise_on is not None and key == self._raise_on:
            raise RuntimeError("simulated MinIO stream failure")
        # Yield in two chunks to exercise the chunked reader.
        data = self._blobs[key]
        yield data[: len(data) // 2]
        yield data[len(data) // 2 :]


def _att(
    *,
    filename: str,
    s3_key: str,
    size_bytes: int,
    skipped_too_large: bool = False,
    content_type: str = "application/octet-stream",
) -> SimpleNamespace:
    return SimpleNamespace(
        filename=filename,
        s3_key=s3_key,
        size_bytes=size_bytes,
        skipped_too_large=skipped_too_large,
        content_type=content_type,
    )


class TestResolveAttachmentParts:
    async def test_included_attachment_streams_bytes(self) -> None:
        storage: Any = _FakeStorage({"k1": b"hello world"})
        atts = [_att(filename="a.txt", s3_key="k1", size_bytes=11)]
        parts = await _resolve_attachment_parts(storage, atts, max_total_bytes=1000)
        assert len(parts) == 1
        assert parts[0].data == b"hello world"
        assert parts[0].filename == "a.txt"

    async def test_skipped_too_large_yields_none_data(self) -> None:
        storage: Any = _FakeStorage({})
        atts = [_att(filename="big.zip", s3_key="k1", size_bytes=999, skipped_too_large=True)]
        parts = await _resolve_attachment_parts(storage, atts, max_total_bytes=10_000)
        assert parts[0].data is None  # not streamed

    async def test_over_budget_attachment_yields_none_data(self) -> None:
        storage: Any = _FakeStorage({"k1": b"x" * 100})
        atts = [_att(filename="big.bin", s3_key="k1", size_bytes=100)]
        # Budget smaller than the attachment → skipped (data None).
        parts = await _resolve_attachment_parts(storage, atts, max_total_bytes=50)
        assert parts[0].data is None

    async def test_running_total_enforced_across_attachments(self) -> None:
        storage: Any = _FakeStorage({"k1": b"a" * 40, "k2": b"b" * 40})
        atts = [
            _att(filename="first.bin", s3_key="k1", size_bytes=40),
            _att(filename="second.bin", s3_key="k2", size_bytes=40),
        ]
        # Budget fits the first (40) but not the running total for the second.
        parts = await _resolve_attachment_parts(storage, atts, max_total_bytes=50)
        assert parts[0].data == b"a" * 40
        assert parts[1].data is None

    async def test_stream_failure_propagates(self) -> None:
        # A MinIO stream failure must bubble up so the caller records an error
        # (mark_error) instead of leaving an orphan claim — no silent swallow.
        storage: Any = _FakeStorage({"k1": b"data"}, raise_on="k1")
        atts = [_att(filename="a.txt", s3_key="k1", size_bytes=4)]
        with pytest.raises(RuntimeError, match="simulated MinIO stream failure"):
            await _resolve_attachment_parts(storage, atts, max_total_bytes=1000)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


class TestSmallHelpers:
    def test_as_aware_adds_utc_to_naive(self) -> None:
        naive = _dt.datetime(2026, 1, 1, 0, 0)
        assert _as_aware(naive).tzinfo is _dt.UTC

    def test_as_aware_keeps_existing_tz(self) -> None:
        aware = _dt.datetime(2026, 1, 1, tzinfo=_dt.UTC)
        assert _as_aware(aware) == aware

    def test_safe_forward_error_strips_newlines_and_clamps(self) -> None:
        exc = ValueError("line1\r\nline2 " + "z" * 1000)
        out = _safe_forward_error(exc, max_len=50)
        assert "\n" not in out and "\r" not in out
        assert out.startswith("ValueError:")
        assert len(out) <= 50


# ---------------------------------------------------------------------------
# Enqueue-side loop guard
# ---------------------------------------------------------------------------


class TestCarriesOwnForwardStamp:
    def test_stamped_message_detected(self) -> None:
        from worker.app.sync_cycle import _carries_own_forward_stamp

        fmsg = SimpleNamespace(x_forwarded_by="mail-aggregator")
        assert _carries_own_forward_stamp(fmsg) is True  # type: ignore[arg-type]

    def test_case_insensitive(self) -> None:
        from worker.app.sync_cycle import _carries_own_forward_stamp

        fmsg = SimpleNamespace(x_forwarded_by="Mail-Aggregator")
        assert _carries_own_forward_stamp(fmsg) is True  # type: ignore[arg-type]

    def test_absent_stamp(self) -> None:
        from worker.app.sync_cycle import _carries_own_forward_stamp

        assert _carries_own_forward_stamp(SimpleNamespace(x_forwarded_by=None)) is False  # type: ignore[arg-type]
        assert _carries_own_forward_stamp(SimpleNamespace(x_forwarded_by="other")) is False  # type: ignore[arg-type]
