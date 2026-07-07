"""Nudge Governor: precision/recall, shadow/live, idempotency, feedback, device-care."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from _engine_util import battery, make_deadline, make_task

from cadence.engine.attention import AttentionSnapshot, AttentionState
from cadence.engine.governor import NudgeGovernor, _Candidate  # noqa: SLF001 (test-only)
from cadence.engine.priority import Misallocation, PriorityEngine, PriorityItem
from cadence.obs.alarms import get_alarm_sink
from cadence.spikes.s0_2.calibration import CalibrationReport, ClassCounts
from cadence.stores.models import Feedback, Nudge

NOW = datetime(2026, 7, 7, 15, 0, tzinfo=UTC)

_MISALLOCATION = "attention.misallocation"
_DEADLINE_REMINDER = "deadline.reminder"  # must match GovernorConfig.deadline_derived_kinds


def _snapshot(target: str) -> AttentionSnapshot:
    return AttentionSnapshot(
        state=AttentionState.IMMERSED, now=NOW, focus_target=target,
        focus_duration=timedelta(minutes=25), confidence=0.9,
    )


def _canonical_finding(store) -> tuple[AttentionSnapshot, Misallocation]:
    refactor = make_task(
        store, "Refactor legacy billing module", priority=1, source_event_ids=["gh-refactor"]
    )
    report = make_task(
        store, "Ship Q3 investor report", priority=4, source_event_ids=["gh-report"]
    )
    make_deadline(store, refactor.id, NOW + timedelta(days=7), source_event_ids=["dl-refactor"])
    make_deadline(store, report.id, NOW + timedelta(days=1), source_event_ids=["dl-report"])
    engine = PriorityEngine(store)
    snap = _snapshot("billing module refactor")
    findings = engine.detect_misallocation(snap, engine.rank(NOW))
    assert findings, "expected a canonical misallocation"
    return snap, findings[0]


def _weak_finding(gap: float = 0.05, confidence: float = 0.5) -> Misallocation:
    a = PriorityItem("a", "task", "A", None, 0.5, 0.5, 0.50)
    b = PriorityItem("b", "task", "B", None, 0.6, 0.5, 0.50 + gap)
    return Misallocation(focused_item=a, neglected_item=b, gap=gap, confidence=confidence, now=NOW)


def test_governor_fires_on_misallocation_live(store) -> None:
    snap, finding = _canonical_finding(store)
    gov = NudgeGovernor(store, mode="live")
    nudge = gov.consider_misallocation(finding, snap, NOW)
    assert nudge is not None and nudge.kind == _MISALLOCATION
    with store.session() as session:
        rows = session.query(Nudge).all()
        assert len(rows) == 1
        assert rows[0].delivered_at is not None  # live = deliverable/committed


def test_governor_suppressed_in_shadow_mode(store) -> None:
    snap, finding = _canonical_finding(store)
    gov = NudgeGovernor(store, mode="shadow")
    proposed = gov.consider_misallocation(finding, snap, NOW)
    assert proposed is not None  # still *proposed*
    assert gov.proposals and gov.proposals[0].nudge_id is None
    with store.session() as session:
        assert session.query(Nudge).count() == 0  # but NOT delivered/persisted


def test_governor_dedupes_via_idempotency_key(store) -> None:
    snap, finding = _canonical_finding(store)
    gov = NudgeGovernor(store, mode="live")
    first = gov.consider_misallocation(finding, snap, NOW)
    second = gov.consider_misallocation(finding, snap, NOW)  # re-eval, same condition
    assert first is not None and second is None
    assert gov.duplicates == 1
    with store.session() as session:
        assert session.query(Nudge).count() == 1


def test_thanks_and_dismiss_adjust_threshold(store) -> None:
    snap, finding = _canonical_finding(store)
    gov = NudgeGovernor(store, mode="live")
    nudge = gov.consider_misallocation(finding, snap, NOW)
    base = gov._threshold(_MISALLOCATION)  # noqa: SLF001

    gov.record_feedback(nudge.nudge_id, "dismiss")
    raised = gov._threshold(_MISALLOCATION)  # noqa: SLF001
    assert raised > base  # Dismiss raises the bar

    gov.record_feedback(nudge.nudge_id, "thanks")
    lowered = gov._threshold(_MISALLOCATION)  # noqa: SLF001
    assert lowered < raised  # Thanks lowers it again

    with store.session() as session:
        signals = {f.signal for f in session.query(Feedback).all()}
        assert signals == {"thanks", "dismiss"}


def test_thanks_rate_and_recall_estimate_compute(store) -> None:
    snap, finding = _canonical_finding(store)
    gov = NudgeGovernor(store, mode="live")
    assert gov.thanks_rate() is None  # no feedback yet
    assert gov.recall_estimate() == 1.0  # nothing evaluated → no misses

    nudge = gov.consider_misallocation(finding, snap, NOW)      # fires
    gov.consider_misallocation(_weak_finding(), None, NOW)       # suppressed (low confidence)
    assert gov.fired == 1 and gov.suppressed == 1
    assert gov.recall_estimate() == 0.5

    gov.record_feedback(nudge.nudge_id, "thanks")
    gov.record_feedback(nudge.nudge_id, "dismiss")
    assert gov.thanks_rate() == 0.5


def test_recall_floor_fires_strong_signal_despite_raised_threshold(store) -> None:
    gov = NudgeGovernor(store, mode="live")
    # Hammer the threshold up to its ceiling with dismissals.
    for _ in range(10):
        gov._adjust(_MISALLOCATION, gov.config.dismiss_delta)  # noqa: SLF001
    assert gov._threshold(_MISALLOCATION) == gov.config.recall_ceiling  # noqa: SLF001

    snap, finding = _canonical_finding(store)  # confidence ~0.9 >= recall floor 0.85
    assert finding.confidence >= gov.config.recall_floor_confidence
    nudge = gov.consider_misallocation(finding, snap, NOW)
    assert nudge is not None  # recall floor guarantees a strong signal still fires


def test_device_care_fires_on_low_battery(store) -> None:
    battery(store, 8, NOW, device="phone")
    gov = NudgeGovernor(store, mode="live")
    emitted = gov.consider_device_care(NOW)
    assert len(emitted) == 1
    assert emitted[0].kind == "device.care"
    assert "8%" in emitted[0].message_summary
    with store.session() as session:
        assert session.query(Nudge).filter(Nudge.kind == "device.care").count() == 1


def test_device_care_silent_when_battery_healthy_or_charging(store) -> None:
    battery(store, 80, NOW, device="phone")
    battery(store, 5, NOW, device="tablet", summary="charging")
    gov = NudgeGovernor(store, mode="live")
    assert gov.consider_device_care(NOW) == []


def test_device_care_is_idempotent_same_day(store) -> None:
    battery(store, 8, NOW, device="phone")
    gov = NudgeGovernor(store, mode="live")
    gov.consider_device_care(NOW)
    battery(store, 6, NOW + timedelta(minutes=5), device="phone")
    gov.consider_device_care(NOW + timedelta(minutes=5))  # same device, same day
    with store.session() as session:
        assert session.query(Nudge).filter(Nudge.kind == "device.care").count() == 1


def test_nudge_carries_provenance_and_no_raw_violation(store) -> None:
    from cadence.stores.models import Fact

    snap, finding = _canonical_finding(store)
    gov = NudgeGovernor(store, mode="live")
    nudge = gov.consider_misallocation(finding, snap, NOW)
    with store.session() as session:
        row = session.get(Nudge, nudge.nudge_id)
        assert row.fact_id is not None  # provenance flows via the linked fact
        fact = session.get(Fact, row.fact_id)
        assert set(fact.source_event_ids) >= {"gh-refactor", "gh-report"}
    assert get_alarm_sink().count("raw_to_cloud_violation") == 0


# --- feedback idempotency + threshold-update concurrency (M5 hardening) --------- #


def test_feedback_adjusts_threshold_once_per_nudge(store) -> None:
    """A double-tap / replayed callback records both rows but moves the bar only once."""
    gov = NudgeGovernor(store, mode="live")
    snap, finding = _canonical_finding(store)
    nudge = gov.consider_misallocation(finding, snap, NOW)
    assert nudge is not None and nudge.nudge_id is not None
    category = nudge.kind

    before = gov._threshold(category)  # noqa: SLF001
    gov.record_feedback(nudge.nudge_id, "dismiss")
    after_one = gov._threshold(category)  # noqa: SLF001
    assert after_one > before  # a dismiss raises the bar

    gov.record_feedback(nudge.nudge_id, "dismiss")  # replay / double-tap
    after_two = gov._threshold(category)  # noqa: SLF001
    assert after_two == after_one  # applied once only — no threshold poisoning

    # Both feedback rows are still persisted for the audit trail.
    with store.session() as session:
        assert (
            session.query(Feedback).filter(Feedback.nudge_id == nudge.nudge_id).count() == 2
        )


def test_threshold_update_is_atomic_under_concurrency(store) -> None:
    """Concurrent `_adjust` calls (scheduler read vs feedback-thread write) lose no update.

    A tiny GIL switch interval forces frequent thread switches mid critical-section, so an
    *unlocked* read-modify-write would demonstrably lose updates (verified: ~3.7k/16k); the
    lock makes the accumulation exact. This is a real regression guard, not a timing fluke.
    """
    import sys
    import threading

    from cadence.engine.governor import GovernorConfig

    # Wide clamps so the accumulation is exact — this isolates the read-modify-write
    # atomicity from the [min, ceiling] clamping.
    gov = NudgeGovernor(
        store,
        mode="live",
        config=GovernorConfig(base_threshold=0.0, min_threshold=-1e9, recall_ceiling=1e9),
    )
    n_threads, per_thread = 8, 2000

    def worker() -> None:
        for _ in range(per_thread):
            gov._adjust("k", 1.0)  # noqa: SLF001

    prev_interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        sys.setswitchinterval(prev_interval)

    assert gov.thresholds["k"] == float(n_threads * per_thread)


# --- S0.2 shadow->live gate, deadline-derived nudges only (M7-A3) --------------- #


def _deadline_candidate(basis_suffix: str) -> _Candidate:
    return _Candidate(
        kind=_DEADLINE_REMINDER,
        confidence=0.99,
        priority=1,
        message_summary="A deadline is coming up.",
        idempotency_basis=(_DEADLINE_REMINDER, basis_suffix),
    )


def _passing_calibration_report() -> CalibrationReport:
    """A synthetic report that clears S0.2's default floors (n>=200, precision>=0.90,
    recall>=0.70 on the evaluated classes)."""
    return CalibrationReport(
        n_events=250,
        overall_accuracy=0.95,
        per_class={
            "no_deadline": ClassCounts(tp=100, fp=5, fn=3),
            "explicit": ClassCounts(tp=80, fp=3, fn=2),
            "inferred": ClassCounts(tp=60, fp=4, fn=3),
        },
        calibration_buckets=[],
        due_at_agreement_rate=0.95,
        divergence_recall=0.9,
    )


def _failing_calibration_report() -> CalibrationReport:
    """A synthetic report with enough volume but below the recall floor for 'inferred'."""
    return CalibrationReport(
        n_events=250,
        overall_accuracy=0.7,
        per_class={
            "no_deadline": ClassCounts(tp=100, fp=5, fn=3),
            "explicit": ClassCounts(tp=80, fp=3, fn=2),
            "inferred": ClassCounts(tp=20, fp=4, fn=40),  # recall well below the 0.70 floor
        },
        calibration_buckets=[],
        due_at_agreement_rate=0.6,
        divergence_recall=0.5,
    )


def test_deadline_gate_blocks_when_no_calibration_source_wired(store) -> None:
    """Default posture: no data feed wired -> insufficient_data -> stays shadow, even in
    live mode."""
    gov = NudgeGovernor(store, mode="live")
    decision = gov.deadline_go_no_go()
    assert decision.verdict == "insufficient_data"

    result = gov._emit(_deadline_candidate("blocked"), None, NOW)  # noqa: SLF001
    assert result is not None  # still proposed (shadow)
    assert result.nudge_id is None  # NOT persisted live
    with store.session() as session:
        assert session.query(Nudge).filter(Nudge.kind == _DEADLINE_REMINDER).count() == 0


def test_deadline_gate_blocks_on_failing_calibration(store) -> None:
    """A wired-but-failing calibration source also keeps deadline nudges shadow."""
    gov = NudgeGovernor(
        store, mode="live", deadline_calibration_source=_failing_calibration_report
    )
    assert gov.deadline_go_no_go().verdict == "no_go"

    result = gov._emit(_deadline_candidate("no-go"), None, NOW)  # noqa: SLF001
    assert result is not None
    assert result.nudge_id is None
    with store.session() as session:
        assert session.query(Nudge).filter(Nudge.kind == _DEADLINE_REMINDER).count() == 0


def test_deadline_gate_opens_on_passing_calibration(store) -> None:
    """A synthetic calibration set clearing the floors opens the gate: deadline-derived
    nudges are now persisted live."""
    gov = NudgeGovernor(
        store, mode="live", deadline_calibration_source=_passing_calibration_report
    )
    assert gov.deadline_go_no_go().verdict == "go"

    result = gov._emit(_deadline_candidate("go"), None, NOW)  # noqa: SLF001
    assert result is not None
    assert result.nudge_id is not None
    with store.session() as session:
        row = session.query(Nudge).filter(Nudge.kind == _DEADLINE_REMINDER).one()
        assert row.delivered_at is not None


def test_deadline_gate_does_not_scope_misallocation_or_device_care(store) -> None:
    """Critical honesty-scoping test: S0.2 measures only the deadline extractor, so this
    gate must never promote OR block priority/misallocation or device-care nudges --
    those stay governed by `mode` alone, in both gate directions."""
    misalloc_candidate = _Candidate(
        kind=_MISALLOCATION, confidence=0.95, priority=1,
        message_summary="switch to the higher-priority item",
        idempotency_basis=(_MISALLOCATION, "scoping-test"),
    )
    care_candidate = _Candidate(
        kind="device.care", confidence=1.0, priority=1,
        message_summary="battery low", idempotency_basis=("device.care", "scoping-test"),
    )

    # Gate wide open ("go") must not be REQUIRED for non-deadline kinds to deliver live --
    # they already do under `mode="live"` alone, same as before this gate existed.
    gov_open = NudgeGovernor(
        store, mode="live", deadline_calibration_source=_passing_calibration_report
    )
    assert gov_open.deadline_go_no_go().verdict == "go"
    fired_open = gov_open._emit(misalloc_candidate, None, NOW)  # noqa: SLF001
    care_open = gov_open._emit(care_candidate, None, NOW)  # noqa: SLF001
    assert fired_open is not None and fired_open.nudge_id is not None
    assert care_open is not None and care_open.nudge_id is not None

    # Gate closed (default: no source wired -> insufficient_data) must not BLOCK them
    # either -- non-deadline kinds are simply outside this gate's authority either way.
    misalloc_candidate_2 = _Candidate(
        kind=_MISALLOCATION, confidence=0.95, priority=1,
        message_summary="switch to the higher-priority item",
        idempotency_basis=(_MISALLOCATION, "scoping-test-2"),
    )
    care_candidate_2 = _Candidate(
        kind="device.care", confidence=1.0, priority=1,
        message_summary="battery low", idempotency_basis=("device.care", "scoping-test-2"),
    )
    gov_closed = NudgeGovernor(store, mode="live")  # no deadline_calibration_source wired
    assert gov_closed.deadline_go_no_go().verdict == "insufficient_data"
    fired_closed = gov_closed._emit(misalloc_candidate_2, None, NOW)  # noqa: SLF001
    care_closed = gov_closed._emit(care_candidate_2, None, NOW)  # noqa: SLF001
    assert fired_closed is not None and fired_closed.nudge_id is not None
    assert care_closed is not None and care_closed.nudge_id is not None
