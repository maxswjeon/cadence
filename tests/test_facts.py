"""Tests for the provenance fact graph (write path, dedupe, NAS evidence, feedback)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from cadence.brain.facts import FactGraph, FactInput, compute_dedupe_key
from cadence.stores.nas import NASStore


def test_assert_fact_writes_structured_row_and_nas_pointer(store, settings) -> None:
    nas = NASStore(settings)
    graph = FactGraph(store, nas)
    fact = graph.assert_fact(
        FactInput(
            kind="deadline_hint",
            subject_type="task",
            subject_id="t1",
            predicate="due",
            object_label="friday",
            confidence_value=0.8,
            confidence_type="heuristic",
            source_event_ids=["e1"],
            summary="task t1 due friday",
            raw_evidence=b"verbatim evidence that stays in NAS",
            expires_at=datetime.now(tz=UTC) + timedelta(days=7),
        )
    )
    assert fact.raw_evidence_id is not None
    # Evidence is retrievable from NAS by the stored pointer.
    assert nas.get(fact.raw_evidence_id) == b"verbatim evidence that stays in NAS"
    assert fact.source_event_ids == ["e1"]
    assert fact.confidence_value == 0.8


def test_dedupe_merges_provenance(store) -> None:
    graph = FactGraph(store)
    spec = FactInput(kind="k", subject_id="s", predicate="p", object_label="o",
                     source_event_ids=["e1"], confidence_value=0.5)
    f1 = graph.assert_fact(spec)
    f2 = graph.assert_fact(
        FactInput(kind="k", subject_id="s", predicate="p", object_label="o",
                  source_event_ids=["e2"], confidence_value=0.9)
    )
    assert f1.id == f2.id  # same dedupe key → same row
    assert set(f2.source_event_ids) == {"e1", "e2"}
    assert f2.confidence_value == 0.9

    # only one row exists
    from cadence.stores.models import Fact
    with store.session() as session:
        assert session.query(Fact).count() == 1


def test_dedupe_key_is_stable() -> None:
    a = compute_dedupe_key("k", "t", "1", "p", "o")
    b = compute_dedupe_key("k", "t", "1", "p", "o")
    assert a == b


def test_add_feedback(store) -> None:
    graph = FactGraph(store)
    fact = graph.assert_fact(FactInput(kind="k", subject_id="s"))
    fb = graph.add_feedback(fact.id, "confirm", weight=1.0, note_summary="looks right")
    assert fb.fact_id == fact.id
    from cadence.stores.models import Feedback
    with store.session() as session:
        assert session.query(Feedback).count() == 1
