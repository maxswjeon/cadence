"""Attention-state classification (engine Component 1)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from _engine_util import activity, spread_activity

from cadence.brain.facts import FactGraph
from cadence.engine.attention import AttentionDetector, AttentionState
from cadence.obs.alarms import get_alarm_sink
from cadence.stores.models import Fact

NOW = datetime(2026, 7, 7, 15, 0, tzinfo=UTC)


def _mins(n: int) -> timedelta:
    return timedelta(minutes=n)


def test_immersed_on_sustained_single_focus(store) -> None:
    spread_activity(store, "vscode", NOW, count=11, step_seconds=120)  # 20 min on one app
    snap = AttentionDetector(store).snapshot(NOW)
    assert snap.state is AttentionState.IMMERSED
    assert snap.focus_target == "vscode"
    assert snap.focus_duration is not None and snap.focus_duration >= _mins(19)
    assert 0.0 <= snap.confidence <= 1.0 and snap.confidence >= 0.7
    assert snap.evidence_fact_ids  # provenance: which facts drove it


def test_scattered_on_frequent_context_switches(store) -> None:
    # Real work across several apps, ~3 min each → many switches, dwell above limbo floor.
    for offset, target in [(18, "A"), (15, "B"), (12, "A"), (9, "C"), (6, "B"), (3, "A"), (0, "B")]:
        activity(store, target, NOW - _mins(offset))
    snap = AttentionDetector(store).snapshot(NOW)
    assert snap.state is AttentionState.SCATTERED


def test_idle_what_next_limbo_on_rapid_flailing(store) -> None:
    # Switching every 30s across many apps, nothing held → "what-next" limbo → IDLE.
    seq = ["A", "B", "C", "D", "A", "B", "C", "D", "A", "B"]
    for i, target in enumerate(seq):
        activity(store, target, NOW - timedelta(seconds=30 * (len(seq) - 1 - i)))
    snap = AttentionDetector(store).snapshot(NOW)
    assert snap.state is AttentionState.IDLE
    assert snap.focus_target is None
    assert "limbo" in snap.reason


def test_idle_when_no_recent_activity(store) -> None:
    activity(store, "vscode", NOW - _mins(12))  # last activity 12 min ago (> idle gap)
    snap = AttentionDetector(store).snapshot(NOW)
    assert snap.state is AttentionState.IDLE


def test_idle_when_no_activity_at_all(store) -> None:
    snap = AttentionDetector(store).snapshot(NOW)
    assert snap.state is AttentionState.IDLE
    assert snap.focus_target is None


def test_non_activity_facts_are_ignored(store) -> None:
    # A non-activity fact inside the window must not be read as focus.
    other = Fact(
        kind="github.issue", subject_type="source_event", subject_id="gh-1",
        object_label="Fix login", dedupe_key="k-github-1",
    )
    other.created_at = NOW - _mins(2)
    store.write(other)
    snap = AttentionDetector(store).snapshot(NOW)
    assert snap.state is AttentionState.IDLE  # no *activity* facts → idle


def test_snapshot_persists_attention_fact_with_provenance(store) -> None:
    spread_activity(store, "figma", NOW, count=11, step_seconds=120)
    detector = AttentionDetector(store, facts=FactGraph(store))
    snap = detector.snapshot(NOW, persist=True)

    with store.session() as session:
        af = session.query(Fact).filter(Fact.kind == "attention.state").one()
        assert af.object_label == snap.state.value
        assert af.source_event_ids  # evidence fact ids carried as provenance
        assert af.summary and "attention=" in af.summary
    # engine writes only structured/summary — no raw-to-cloud violation
    assert get_alarm_sink().count("raw_to_cloud_violation") == 0
