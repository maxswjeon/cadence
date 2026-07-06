"""Tests for the ingestion pipeline: round-trip, device dedupe, backpressure, deadline hook."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from cadence.adapters.base import AcquisitionTier, Event
from cadence.brain.deadlines import DeadlineCandidate, DeadlineExtractor
from cadence.ingest.pipeline import (
    BackpressureError,
    IngestPipeline,
    WALBuffer,
)
from cadence.stores.models import Deadline, Fact


def _event(event_id="e1", **kw) -> Event:
    return Event(
        event_id=event_id,
        source="github",
        account_ref="octocat",
        kind="github.issue",
        acquisition_tier=AcquisitionTier.OFFICIAL_API,
        summary="issue: fix bug",
        confidence=0.9,
        **kw,
    )


def test_ingest_round_trip_creates_fact(store) -> None:
    pipe = IngestPipeline(store)
    result = pipe.ingest(_event())
    assert result.accepted and result.fact_id
    with store.session() as session:
        fact = session.get(Fact, result.fact_id)
        assert fact is not None
        assert fact.source_event_ids == ["e1"]


def test_device_dedupe_skips_duplicates(store) -> None:
    pipe = IngestPipeline(store)
    first = pipe.ingest(_event(device_id="phone"))
    second = pipe.ingest(_event(device_id="laptop"))  # same event, other device
    assert first.accepted
    assert second.duplicate and not second.accepted
    with store.session() as session:
        assert session.query(Fact).count() == 1


def test_backpressure_raises_when_wal_full(store) -> None:
    pipe = IngestPipeline(store, wal=WALBuffer(max_depth=0))
    with pytest.raises(BackpressureError):
        pipe.ingest(_event())


def test_wal_buffer_tracks_offset_and_drains() -> None:
    wal = WALBuffer(max_depth=5)
    o1 = wal.append(_event("a"))
    o2 = wal.append(_event("b"))
    assert (o1, o2) == (1, 2)
    assert wal.depth == 2
    drained = wal.drain()
    assert len(drained) == 2 and wal.depth == 0


class _FixedDeadlineExtractor(DeadlineExtractor):
    """Test double for the (later-wave) deadline parser hook point."""

    def extract(self, event: Event) -> list[DeadlineCandidate]:
        return [
            DeadlineCandidate(
                due_at=datetime(2026, 7, 10, 17, 0, tzinfo=UTC),
                origin="explicit",
                confidence_value=1.0,
                confidence_type="source",
                source_event_ids=[event.event_id],
                summary="due 2026-07-10",
            )
        ]


def test_deadline_extractor_hook_writes_deadline_rows(store) -> None:
    pipe = IngestPipeline(store, deadline_extractor=_FixedDeadlineExtractor())
    result = pipe.ingest(_event())
    assert result.deadlines_created == 1
    with store.session() as session:
        dl = session.query(Deadline).one()
        assert dl.origin == "explicit"
        assert dl.source_event_ids == ["e1"]
