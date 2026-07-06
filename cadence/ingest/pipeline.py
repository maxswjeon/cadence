"""Ingestion pipeline.

Append-only intake that turns provenance-tagged :class:`~cadence.adapters.base.Event`
objects into fact-graph rows in D1. Responsibilities:

* **Device dedupe** — every device is an independent WAL producer; events are deduped
  on their cross-device ``dedupe_id`` so a message seen on two devices lands once.
* **WAL buffer + backpressure** — :class:`WALBuffer` is an append-only buffer with a
  bounded depth; when full it raises :class:`BackpressureError` (the durable WAL and
  reconciliation authority replace the in-memory stub in a later milestone).
* **Routing** — each accepted event becomes a fact in the provenance graph
  (:class:`~cadence.brain.facts.FactGraph`), and the configured
  :class:`~cadence.brain.deadlines.DeadlineExtractor` derives ``deadline`` rows.

The pipeline never writes verbatim raw content to D1 — raw evidence goes to NAS and the
fact carries only the pointer + a non-verbatim summary.

**Concurrency:** a single :class:`IngestPipeline` instance is shared across requests
(e.g. one instance in ``app.state.pipeline``), and FastAPI runs sync route handlers in
a thread pool. :meth:`IngestPipeline.ingest` therefore serializes its whole
dedupe-check → WAL-append → fact/projection/deadline-write critical section behind an
instance lock, so ``_seen`` and :class:`WALBuffer` never see interleaved writes from
concurrent requests.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

from cadence.adapters.base import Event
from cadence.brain.deadlines import DeadlineExtractor, NullDeadlineExtractor
from cadence.brain.facts import FactGraph, FactInput
from cadence.brain.projection import (
    ProjectionContext,
    ProjectionRegistry,
    default_projection_registry,
)
from cadence.obs.logging import get_logger, log_event
from cadence.stores.d1 import D1Store
from cadence.stores.models import Deadline

_log = get_logger("ingest.pipeline")


class BackpressureError(RuntimeError):
    """Raised when the WAL buffer is full and cannot accept more events."""


@dataclass
class WALBuffer:
    """Bounded append-only write-ahead buffer (in-memory stub with backpressure).

    Interface mirrors what a durable, offset-tracked WAL will expose. ``offset`` is a
    monotonic counter of accepted appends; ``drain`` empties the pending records.
    """

    max_depth: int = 10_000
    _pending: list[Event] = field(default_factory=list)
    offset: int = 0

    @property
    def depth(self) -> int:
        return len(self._pending)

    def append(self, event: Event) -> int:
        if len(self._pending) >= self.max_depth:
            raise BackpressureError(
                f"WAL buffer full (depth={len(self._pending)} >= max_depth={self.max_depth})"
            )
        self._pending.append(event)
        self.offset += 1
        return self.offset

    def drain(self) -> list[Event]:
        drained, self._pending = self._pending, []
        return drained


@dataclass
class IngestResult:
    """Outcome of ingesting a single event."""

    dedupe_id: str
    accepted: bool
    duplicate: bool = False
    fact_id: str | None = None
    deadlines_created: int = 0
    wal_offset: int | None = None
    #: Typed rows projected from the event, as ``(table_name, row_id)`` pairs.
    projected: list[tuple[str, str]] = field(default_factory=list)


class IngestPipeline:
    """Routes events → fact graph → D1, with device dedupe and a WAL buffer."""

    def __init__(
        self,
        d1: D1Store,
        *,
        fact_graph: FactGraph | None = None,
        deadline_extractor: DeadlineExtractor | None = None,
        projection_registry: ProjectionRegistry | None = None,
        wal: WALBuffer | None = None,
    ) -> None:
        self.d1 = d1
        self.facts = fact_graph or FactGraph(d1)
        self.deadline_extractor = deadline_extractor or NullDeadlineExtractor()
        self.projection = projection_registry or default_projection_registry()
        self._projection_ctx = ProjectionContext(d1)
        self.wal = wal or WALBuffer()
        self._seen: set[str] = set()
        self._lock = threading.Lock()

    def ingest(self, event: Event) -> IngestResult:
        """Ingest one event (idempotent on ``dedupe_id``).

        The whole method body runs under :attr:`_lock` so concurrent callers (e.g.
        two threadpool-served FastAPI requests) cannot interleave the dedupe
        check, the WAL append, or the downstream fact/projection/deadline writes.
        """
        event = event.with_dedupe_id()
        dedupe_id = event.dedupe_id or ""

        with self._lock:
            if dedupe_id in self._seen:
                log_event(_log, 20, "ingest.duplicate", dedupe_id=dedupe_id, source=event.source)
                return IngestResult(dedupe_id=dedupe_id, accepted=False, duplicate=True)

            offset = self.wal.append(event)

            fact = self.facts.assert_fact(
                FactInput(
                    kind=event.kind,
                    subject_type="source_event",
                    subject_id=event.event_id,
                    predicate="observed",
                    object_label=event.source,
                    confidence_value=event.confidence,
                    confidence_type="observed" if event.confidence is not None else None,
                    source_event_ids=[event.event_id],
                    summary=event.summary,
                    raw_evidence_id=event.raw_evidence_ref,
                    raw_evidence_hash=event.payload_hash,
                )
            )

            projected = self._project(event)
            deadlines_created = self._route_deadlines(event)

            self._seen.add(dedupe_id)
            self.wal.drain()
            log_event(
                _log,
                20,
                "ingest.accepted",
                dedupe_id=dedupe_id,
                source=event.source,
                fact_id=fact.id,
                deadlines=deadlines_created,
                projected=[t for t, _ in projected],
            )
            return IngestResult(
                dedupe_id=dedupe_id,
                accepted=True,
                fact_id=fact.id,
                deadlines_created=deadlines_created,
                wal_offset=offset,
                projected=projected,
            )

    def _project(self, event: Event) -> list[tuple[str, str]]:
        """Project the event into typed D1 rows (in addition to its Fact)."""
        rows = self.projection.project(event, self._projection_ctx)
        if not rows:
            return []
        self.d1.write_all(rows)
        return [(type(row).__tablename__, row.id) for row in rows]

    def _route_deadlines(self, event: Event) -> int:
        candidates = self.deadline_extractor.extract(event)
        rows: list[object] = []
        for cand in candidates:
            rows.append(
                Deadline(
                    due_at=cand.due_at,
                    origin=cand.origin,
                    divergence_flag=cand.divergence_flag,
                    confidence_value=cand.confidence_value,
                    confidence_type=cand.confidence_type,
                    source_event_ids=cand.source_event_ids or [event.event_id],
                    summary=cand.summary,
                )
            )
        if rows:
            self.d1.write_all(rows)
        return len(rows)

    def ingest_many(self, events: list[Event]) -> list[IngestResult]:
        return [self.ingest(e) for e in events]


__all__ = ["IngestPipeline", "WALBuffer", "IngestResult", "BackpressureError"]
