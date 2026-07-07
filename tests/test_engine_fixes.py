"""Regression tests for the M3 engine fix wave (decision-correctness bugs)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from _engine_util import activity, battery, make_deadline, make_task, spread_activity

from cadence.engine.attention import (
    AttentionConfig,
    AttentionDetector,
    AttentionSnapshot,
    AttentionState,
)
from cadence.engine.engine import AttentionEngine
from cadence.engine.governor import (
    _MISALLOCATION,
    GovernorConfig,
    NudgeGovernor,
    _hash_key,
)
from cadence.engine.priority import Misallocation, PriorityEngine, PriorityItem
from cadence.stores.models import Nudge

NOW = datetime(2026, 7, 7, 15, 0, tzinfo=UTC)


def _immersed_on(target: str, minutes: int = 25) -> AttentionSnapshot:
    return AttentionSnapshot(
        state=AttentionState.IMMERSED, now=NOW, focus_target=target,
        focus_duration=timedelta(minutes=minutes), confidence=0.9,
    )


# --------------------------------------------------------------------------- #
# Fix 1 — priority inversion in misallocation
# --------------------------------------------------------------------------- #


def test_fix1_no_misallocation_when_neglected_is_less_important(store) -> None:
    """High-priority task due in 14d must NOT be abandoned for a low-priority task due 2h."""
    focus = make_task(store, "Deep architecture work", priority=5)
    survey = make_task(store, "Reply to survey", priority=1)
    make_deadline(store, focus.id, NOW + timedelta(days=14))
    make_deadline(store, survey.id, NOW + timedelta(hours=2))
    engine = PriorityEngine(store)
    findings = engine.detect_misallocation(_immersed_on("Deep architecture work"), engine.rank(NOW))
    assert findings == []


def test_fix1_fires_when_neglected_is_more_important(store) -> None:
    focus = make_task(store, "Refactor legacy billing module", priority=1)
    report = make_task(store, "Ship Q3 investor report", priority=5)
    make_deadline(store, focus.id, NOW + timedelta(days=7))
    make_deadline(store, report.id, NOW + timedelta(days=1))
    engine = PriorityEngine(store)
    findings = engine.detect_misallocation(_immersed_on("billing refactor"), engine.rank(NOW))
    assert len(findings) == 1
    assert findings[0].neglected_item.item_id == report.id


# --------------------------------------------------------------------------- #
# Fix 2 — sub-threshold "IMMERSED" must not fire a nudge
# --------------------------------------------------------------------------- #


def test_fix2_brief_glance_does_not_nudge(store) -> None:
    make_task(store, "Refactor legacy billing module", priority=1)
    report = make_task(store, "Ship Q3 investor report", priority=5)
    make_deadline(store, report.id, NOW + timedelta(days=1))
    # A 15-second single-app glance — nowhere near the immersion threshold.
    spread_activity(store, "billing module refactor", NOW, count=2, step_seconds=15)

    detector = AttentionDetector(store)
    snap = detector.snapshot(NOW)
    assert snap.state is AttentionState.IMMERSED  # single target...
    assert snap.confidence < GovernorConfig().min_threshold  # ...but sub-threshold
    assert snap.focus_duration is not None
    assert snap.focus_duration.total_seconds() < detector.config.immersion_seconds

    tick = AttentionEngine(store, NudgeGovernor(store, mode="live")).evaluate(NOW)
    assert tick.misallocations == []
    with store.session() as session:
        assert session.query(Nudge).filter(Nudge.kind == _MISALLOCATION).count() == 0


# --------------------------------------------------------------------------- #
# Fix 3 — governor defaults to shadow mode
# --------------------------------------------------------------------------- #


def test_fix3_governor_defaults_to_shadow(store) -> None:
    gov = NudgeGovernor(store)  # no mode given
    assert gov.mode == "shadow"

    focus = make_task(store, "Refactor legacy billing module", priority=1, source_event_ids=["a"])
    report = make_task(store, "Ship Q3 investor report", priority=5, source_event_ids=["b"])
    make_deadline(store, focus.id, NOW + timedelta(days=7))
    make_deadline(store, report.id, NOW + timedelta(days=1))
    engine = PriorityEngine(store)
    finding = engine.detect_misallocation(_immersed_on("billing refactor"), engine.rank(NOW))[0]
    proposed = gov.consider_misallocation(finding, None, NOW)
    assert proposed is not None  # proposed...
    with store.session() as session:
        assert session.query(Nudge).count() == 0  # ...but nothing delivered by default


# --------------------------------------------------------------------------- #
# Fix 4 — a charging device is not nagged, even with a stale discharge reading
# --------------------------------------------------------------------------- #


def test_fix4_charging_supersedes_stale_low_reading(store) -> None:
    battery(store, 8, NOW - timedelta(minutes=5), device="phone")           # older: discharging 8%
    battery(store, 9, NOW, device="phone", summary="charging")             # newest: plugged in
    gov = NudgeGovernor(store, mode="live")
    assert gov.consider_device_care(NOW) == []
    with store.session() as session:
        assert session.query(Nudge).filter(Nudge.kind == "device.care").count() == 0


def test_fix4_still_fires_when_latest_is_discharging(store) -> None:
    battery(store, 40, NOW - timedelta(minutes=5), device="phone", summary="charging")
    battery(store, 8, NOW, device="phone")  # newest: unplugged and low
    gov = NudgeGovernor(store, mode="live")
    assert len(gov.consider_device_care(NOW)) == 1


# --------------------------------------------------------------------------- #
# Fix 5 — a wide intra-run gap is not continuous immersion
# --------------------------------------------------------------------------- #


def test_fix5_two_samples_20min_apart_are_not_immersion(store) -> None:
    activity(store, "vscode", NOW - timedelta(minutes=20))
    activity(store, "vscode", NOW)
    detector = AttentionDetector(store)
    snap = detector.snapshot(NOW)
    # Must NOT read as 20 minutes of sustained focus.
    assert snap.focus_duration is not None
    assert snap.focus_duration.total_seconds() < detector.config.immersion_seconds
    assert snap.confidence < GovernorConfig().min_threshold


def test_fix5_dense_samples_still_immersed(store) -> None:
    # Sanity: samples inside the gap window still accumulate to real immersion.
    spread_activity(store, "vscode", NOW, count=11, step_seconds=120)
    snap = AttentionDetector(store).snapshot(NOW)
    assert snap.state is AttentionState.IMMERSED
    assert snap.focus_duration.total_seconds() >= AttentionConfig().immersion_seconds


# --------------------------------------------------------------------------- #
# Fix 6 — long-overdue items age out of "imminent"
# --------------------------------------------------------------------------- #


def test_fix6_is_imminent_ages_out_stale_overdue() -> None:
    fresh_overdue = PriorityItem("x", "task", "X", NOW - timedelta(hours=2), 1.0, 0.5, 0.8)
    ancient = PriorityItem("y", "task", "Y", NOW - timedelta(days=10), 1.0, 0.5, 0.8)
    assert fresh_overdue.is_imminent(NOW, 48.0, 168.0) is True
    assert ancient.is_imminent(NOW, 48.0, 168.0) is False


def test_fix6_abandoned_overdue_task_no_longer_neglected(store) -> None:
    focus = make_task(store, "Refactor legacy billing module", priority=1)
    abandoned = make_task(store, "Ship Q3 investor report", priority=5)
    make_deadline(store, focus.id, NOW + timedelta(days=7))
    make_deadline(store, abandoned.id, NOW - timedelta(days=10))  # 10 days overdue → aged out
    engine = PriorityEngine(store)
    assert engine.detect_misallocation(_immersed_on("billing refactor"), engine.rank(NOW)) == []


# --------------------------------------------------------------------------- #
# Fix 7 — re-suppressing the same condition does not collapse recall
# --------------------------------------------------------------------------- #


def _weak_finding() -> Misallocation:
    a = PriorityItem("wa", "task", "A", None, 0.5, 0.5, 0.50)
    b = PriorityItem("wb", "task", "B", None, 0.6, 0.5, 0.55)
    return Misallocation(focused_item=a, neglected_item=b, gap=0.05, confidence=0.5, now=NOW)


def test_fix7_recall_estimate_stable_across_repeated_suppressions(store) -> None:
    focus = make_task(store, "Refactor legacy billing module", priority=1, source_event_ids=["a"])
    report = make_task(store, "Ship Q3 investor report", priority=5, source_event_ids=["b"])
    make_deadline(store, focus.id, NOW + timedelta(days=7))
    make_deadline(store, report.id, NOW + timedelta(days=1))
    pe = PriorityEngine(store)
    strong = pe.detect_misallocation(_immersed_on("billing refactor"), pe.rank(NOW))[0]

    gov = NudgeGovernor(store, mode="live")
    gov.consider_misallocation(strong, None, NOW)         # fires
    for _ in range(5):
        gov.consider_misallocation(_weak_finding(), None, NOW)  # same weak condition, re-suppressed
    assert gov.fired == 1
    assert gov.suppressed == 1  # deduped, not 5
    assert gov.recall_estimate() == 0.5


# --------------------------------------------------------------------------- #
# Fix 8 — a concurrent-insert IntegrityError is treated as a duplicate, not a crash
# --------------------------------------------------------------------------- #


def test_fix8_toctou_integrity_error_is_swallowed(store) -> None:
    focus = make_task(store, "Refactor legacy billing module", priority=1, source_event_ids=["a"])
    report = make_task(store, "Ship Q3 investor report", priority=5, source_event_ids=["b"])
    make_deadline(store, focus.id, NOW + timedelta(days=7))
    make_deadline(store, report.id, NOW + timedelta(days=1))
    pe = PriorityEngine(store)
    finding = pe.detect_misallocation(_immersed_on("billing refactor"), pe.rank(NOW))[0]

    # Pre-insert a nudge with the exact key this finding will compute (simulating a racing
    # tick that committed first) and blind the SELECT-side check so the INSERT is what trips.
    day = finding.now.date().isoformat()
    key = _hash_key(
        (_MISALLOCATION, finding.focused_item.item_id, finding.neglected_item.item_id, day)
    )
    store.write(Nudge(kind=_MISALLOCATION, idempotency_key=key, message_summary="pre"))

    gov = NudgeGovernor(store, mode="live")
    gov._existing_nudge = lambda k: False  # noqa: SLF001 - force the INSERT path
    result = gov.consider_misallocation(finding, None, NOW)  # must not raise
    assert result is None
    assert gov.duplicates == 1
    with store.session() as session:
        assert session.query(Nudge).filter(Nudge.idempotency_key == key).count() == 1


# --------------------------------------------------------------------------- #
# Fix 9 — explicit deadline preferred over an earlier inferred one
# --------------------------------------------------------------------------- #


def test_fix9_explicit_deadline_governs_over_earlier_inferred(store) -> None:
    task = make_task(store, "Quarterly report", priority=3)
    make_deadline(store, task.id, NOW + timedelta(days=1), origin="inferred")
    make_deadline(store, task.id, NOW + timedelta(days=3), origin="explicit")
    view = PriorityEngine(store).rank(NOW)
    item = next(it for it in view.items if it.item_id == task.id)
    assert item.due_at == NOW + timedelta(days=3)  # the explicit one, not the earlier inferred


# --------------------------------------------------------------------------- #
# Fix 10 — unknown-nudge feedback raises; priority band is config-driven
# --------------------------------------------------------------------------- #


def test_fix10_feedback_on_unknown_nudge_raises_and_leaves_thresholds(store) -> None:
    gov = NudgeGovernor(store, mode="live")
    before = dict(gov.thresholds)
    with pytest.raises(ValueError, match="unknown nudge_id"):
        gov.record_feedback("no-such-nudge", "thanks")
    assert gov.thresholds == before


def test_fix10_priority_band_is_config_driven(store) -> None:
    focus = make_task(store, "Refactor legacy billing module", priority=1, source_event_ids=["a"])
    report = make_task(store, "Ship Q3 investor report", priority=5, source_event_ids=["b"])
    make_deadline(store, focus.id, NOW + timedelta(days=7))
    make_deadline(store, report.id, NOW + timedelta(days=1))
    pe = PriorityEngine(store)
    finding = pe.detect_misallocation(_immersed_on("billing refactor"), pe.rank(NOW))[0]
    assert finding.gap >= 0.4  # would be "high" under the default band

    gov = NudgeGovernor(store, mode="live", config=GovernorConfig(high_priority_gap=0.9))
    nudge = gov.consider_misallocation(finding, None, NOW)
    assert nudge.priority == GovernorConfig().misalloc_priority_low  # config raised the bar
