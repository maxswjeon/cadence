"""Provenance fact graph.

A :class:`~cadence.stores.models.Fact` is a derived assertion that always carries its
provenance: ``source_event_ids``, a NAS evidence pointer (``raw_evidence_id`` /
``raw_evidence_hash``), a confidence value + type, an optional expiration, and a
feedback history (via the ``feedback`` table).

:class:`FactGraph` is the write path: it stores any verbatim evidence in NAS, then
writes the **structured** fact row to D1 (raw-boundary enforced by :class:`D1Store`).
Writes are **deduped** by a stable ``dedupe_key`` — re-asserting the same fact merges
provenance (unions ``source_event_ids``, refreshes confidence) instead of duplicating.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select

from cadence.stores.d1 import D1Store
from cadence.stores.models import Fact, Feedback
from cadence.stores.nas import NASStore


def compute_dedupe_key(
    kind: str,
    subject_type: str | None,
    subject_id: str | None,
    predicate: str | None,
    object_label: str | None,
) -> str:
    """Stable content key used to collapse duplicate derivations of the same fact."""
    basis = "|".join(str(x) for x in (kind, subject_type, subject_id, predicate, object_label))
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


@dataclass
class FactInput:
    """Caller-facing description of a fact to assert into the graph."""

    kind: str
    subject_type: str | None = None
    subject_id: str | None = None
    predicate: str | None = None
    object_label: str | None = None
    confidence_value: float | None = None
    confidence_type: str | None = None
    source_event_ids: list[str] = field(default_factory=list)
    expires_at: datetime | None = None
    summary: str | None = None
    #: Optional verbatim evidence bytes — stored in NAS, never in D1.
    raw_evidence: bytes | None = None
    #: Pre-existing NAS pointer (if evidence was already stored elsewhere).
    raw_evidence_id: str | None = None
    raw_evidence_hash: str | None = None
    dedupe_key: str | None = None

    def resolved_dedupe_key(self) -> str:
        return self.dedupe_key or compute_dedupe_key(
            self.kind, self.subject_type, self.subject_id, self.predicate, self.object_label
        )


class FactGraph:
    """Write/dedupe path for the provenance fact graph."""

    def __init__(self, d1: D1Store, nas: NASStore | None = None) -> None:
        self.d1 = d1
        self.nas = nas or NASStore(d1._settings)  # noqa: SLF001 — share settings

    def assert_fact(self, spec: FactInput) -> Fact:
        """Assert a fact, storing evidence in NAS and the structured row in D1 (deduped)."""
        raw_id = spec.raw_evidence_id
        raw_hash = spec.raw_evidence_hash
        if spec.raw_evidence is not None:
            ref = self.nas.put(spec.raw_evidence)
            raw_id, raw_hash = ref.id, ref.hash

        dedupe_key = spec.resolved_dedupe_key()

        with self.d1.session() as session:
            existing = session.execute(
                select(Fact).where(Fact.dedupe_key == dedupe_key)
            ).scalar_one_or_none()
            if existing is not None:
                # Merge provenance: union source events, refresh confidence/summary/evidence.
                prior_ids = existing.source_event_ids or []
                existing.source_event_ids = list(dict.fromkeys(prior_ids + spec.source_event_ids))
                if spec.confidence_value is not None:
                    existing.confidence_value = spec.confidence_value
                    existing.confidence_type = spec.confidence_type
                if spec.summary is not None:
                    existing.summary = spec.summary
                if raw_id is not None:
                    existing.raw_evidence_id = raw_id
                    existing.raw_evidence_hash = raw_hash
                if spec.expires_at is not None:
                    existing.expires_at = spec.expires_at
                session.flush()
                # Enforce the boundary AND enqueue the merged row for replication, so a
                # re-assert reaches the Cloudflare replica (not just the local canonical).
                self.d1.replicate_instance(existing)
                session.expunge(existing)
                return existing

        fact = Fact(
            kind=spec.kind,
            subject_type=spec.subject_type,
            subject_id=spec.subject_id,
            predicate=spec.predicate,
            object_label=spec.object_label,
            confidence_value=spec.confidence_value,
            confidence_type=spec.confidence_type,
            source_event_ids=list(spec.source_event_ids),
            raw_evidence_id=raw_id,
            raw_evidence_hash=raw_hash,
            summary=spec.summary,
            expires_at=spec.expires_at,
            dedupe_key=dedupe_key,
        )
        return self.d1.write(fact)

    def add_feedback(
        self,
        fact_id: str,
        signal: str,
        *,
        weight: float = 1.0,
        note_summary: str | None = None,
    ) -> Feedback:
        """Append a feedback signal to a fact's history."""
        fb = Feedback(fact_id=fact_id, signal=signal, weight=weight, note_summary=note_summary)
        return self.d1.write(fb)

    def get(self, fact_id: str) -> Fact | None:
        with self.d1.session() as session:
            fact = session.get(Fact, fact_id)
            if fact is not None:
                session.expunge(fact)
            return fact


__all__ = ["FactGraph", "FactInput", "compute_dedupe_key"]
