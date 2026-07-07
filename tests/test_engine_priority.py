"""Priority scoring + misallocation detection (engine Component 2)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from _engine_util import make_deadline, make_task, spread_activity

from cadence.engine.attention import (
    AttentionDetector,
    AttentionSnapshot,
    AttentionState,
)
from cadence.engine.priority import PriorityEngine

NOW = datetime(2026, 7, 7, 15, 0, tzinfo=UTC)


def test_urgency_ranks_closer_deadlines_higher(store) -> None:
    soon = make_task(store, "Soon task", priority=2)
    later = make_task(store, "Later task", priority=2)
    make_deadline(store, soon.id, NOW + timedelta(hours=6))
    make_deadline(store, later.id, NOW + timedelta(days=10))
    view = PriorityEngine(store).rank(NOW)
    top = view.top()
    assert top is not None and top.item_id == soon.id
    assert top.urgency > view.items[-1].urgency


def test_closed_tasks_are_excluded(store) -> None:
    make_task(store, "Done", priority=5, status="closed")
    make_task(store, "Open", priority=1)
    view = PriorityEngine(store).rank(NOW)
    assert [it.title for it in view.items] == ["Open"]


def test_explicit_deadline_boosts_importance_over_inferred(store) -> None:
    a = make_task(store, "Explicit due", priority=2)
    b = make_task(store, "Inferred due", priority=2)
    make_deadline(store, a.id, NOW + timedelta(days=2), origin="explicit")
    make_deadline(store, b.id, NOW + timedelta(days=2), origin="inferred")
    view = PriorityEngine(store).rank(NOW)
    scores = {it.title: it.importance for it in view.items}
    assert scores["Explicit due"] > scores["Inferred due"]


def _immersed_on(target: str) -> AttentionSnapshot:
    return AttentionSnapshot(
        state=AttentionState.IMMERSED, now=NOW, focus_target=target,
        focus_duration=timedelta(minutes=25), confidence=0.9,
    )


def test_canonical_7day_vs_1day_misallocation(store) -> None:
    """Immersed in the low-priority 7-day refactor while the 1-day report is due."""
    refactor = make_task(
        store, "Refactor legacy billing module", priority=1, source_event_ids=["gh-refactor"]
    )
    report = make_task(
        store, "Ship Q3 investor report", priority=4, source_event_ids=["gh-report"]
    )
    make_deadline(store, refactor.id, NOW + timedelta(days=7), source_event_ids=["dl-refactor"])
    make_deadline(store, report.id, NOW + timedelta(days=1), source_event_ids=["dl-report"])

    engine = PriorityEngine(store)
    view = engine.rank(NOW)
    # The report outranks the refactor despite the user being deep in the refactor.
    assert view.top().item_id == report.id

    snapshot = _immersed_on("billing-module refactor (editor)")
    findings = engine.detect_misallocation(snapshot, view)
    assert len(findings) == 1
    m = findings[0]
    assert m.focused_item.item_id == refactor.id
    assert m.neglected_item.item_id == report.id
    assert m.gap > 0.4
    assert m.confidence >= 0.85
    assert "Ship Q3 investor report" in m.reason


def test_no_misallocation_when_focused_on_the_top_item(store) -> None:
    refactor = make_task(store, "Refactor legacy billing module", priority=1)
    report = make_task(store, "Ship Q3 investor report", priority=4)
    make_deadline(store, refactor.id, NOW + timedelta(days=7))
    make_deadline(store, report.id, NOW + timedelta(days=1))
    engine = PriorityEngine(store)
    view = engine.rank(NOW)
    # Immersed on the report itself → no misallocation.
    findings = engine.detect_misallocation(_immersed_on("Ship Q3 investor report"), view)
    assert findings == []


def test_no_misallocation_when_not_immersed(store) -> None:
    refactor = make_task(store, "Refactor legacy billing module", priority=1)
    report = make_task(store, "Ship Q3 investor report", priority=4)
    make_deadline(store, refactor.id, NOW + timedelta(days=7))
    make_deadline(store, report.id, NOW + timedelta(days=1))
    engine = PriorityEngine(store)
    view = engine.rank(NOW)
    scattered = AttentionSnapshot(
        state=AttentionState.SCATTERED, now=NOW, focus_target="billing refactor", confidence=0.7
    )
    assert engine.detect_misallocation(scattered, view) == []


def test_no_misallocation_when_higher_item_not_imminent(store) -> None:
    # The higher-priority item is 10 days out — not imminent → no nudge-worthy conflict.
    refactor = make_task(store, "Refactor legacy billing module", priority=1)
    report = make_task(store, "Ship Q3 investor report", priority=4)
    make_deadline(store, refactor.id, NOW + timedelta(days=3))
    make_deadline(store, report.id, NOW + timedelta(days=10))
    engine = PriorityEngine(store)
    view = engine.rank(NOW)
    assert engine.detect_misallocation(_immersed_on("billing refactor"), view) == []


def test_detect_from_live_attention_snapshot(store) -> None:
    """End-to-end: real activity facts → attention snapshot → misallocation."""
    refactor = make_task(store, "Refactor legacy billing module", priority=1)
    report = make_task(store, "Ship Q3 investor report", priority=4)
    make_deadline(store, refactor.id, NOW + timedelta(days=7))
    make_deadline(store, report.id, NOW + timedelta(days=1))
    spread_activity(store, "billing module refactor", NOW, count=11, step_seconds=120)

    snapshot = AttentionDetector(store).snapshot(NOW)
    assert snapshot.state is AttentionState.IMMERSED
    findings = PriorityEngine(store).detect_misallocation(snapshot, PriorityEngine(store).rank(NOW))
    assert len(findings) == 1
    assert findings[0].neglected_item.item_id == report.id
