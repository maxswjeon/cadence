"""Tests for the S0.2 inference-calibration harness spike.

Two layers: a known-answer unit test of the metrics arithmetic (hand-built events with
an exactly-computed expected confusion matrix), and an end-to-end run against the S0.0
synthetic bootstrap sample to prove the harness works — not to assert a real
calibration verdict (see `.omc/research/spikes/s0_2.md`).
"""

from __future__ import annotations

from datetime import UTC, datetime

from cadence.adapters.base import AcquisitionTier, Event
from cadence.brain.deadlines import RuleDeadlineExtractor
from cadence.spikes.s0_0.labeling import GoldLabel
from cadence.spikes.s0_0.sample import build_bootstrap_sample
from cadence.spikes.s0_2.calibration import PRIORITY_NOT_EVALUATED, evaluate
from cadence.spikes.s0_2.thresholds import GoNoGoThresholds, evaluate_go_no_go
from cadence.stores.nas import NASStore


def _event(event_id, summary=None, structured=None, occurred_at=None) -> Event:
    return Event(
        event_id=event_id,
        source="test",
        account_ref="acct",
        acquisition_tier=AcquisitionTier.MANUAL,
        kind="test.item",
        occurred_at=occurred_at or datetime(2026, 7, 1, tzinfo=UTC),
        summary=summary,
        structured=structured or {},
    )


# --------------------------------------------------------------------------- #
# Known-answer metrics arithmetic
# --------------------------------------------------------------------------- #


def test_evaluate_known_answer_confusion_matrix():
    # 1 true positive (explicit), 1 true negative (keyword trap -- the extractor now
    # correctly ties "due" to an adjacent date only, so this one no longer misfires),
    # 1 false negative (a relative-date phrase the extractor still can't parse), 1
    # true negative (plain chatter).
    tp_explicit = _event("tp", structured={"due_at": "2026-07-10T00:00:00+00:00"})
    fp_trap = _event("fp", summary="Shipped on 2026-01-01, nothing else is due right now")
    fn_missed = _event("fn", summary="wrap this up before next Friday")
    tn_chatter = _event("tn", summary="just checking in")

    pairs = [
        (tp_explicit, GoldLabel(dedupe_id="tp", has_deadline=True, origin="explicit")),
        (fp_trap, GoldLabel(dedupe_id="fp", has_deadline=False)),
        (fn_missed, GoldLabel(dedupe_id="fn", has_deadline=True)),
        (tn_chatter, GoldLabel(dedupe_id="tn", has_deadline=False)),
    ]

    report = evaluate(pairs, RuleDeadlineExtractor())

    assert report.n_events == 4
    no_deadline = report.per_class["no_deadline"]
    explicit = report.per_class["explicit"]
    inferred = report.per_class["inferred"]

    # fp_trap and tn_chatter are both correct no_deadline calls -- the extractor's
    # keyword-proximity fix means an unrelated date near "due" no longer fires.
    # fn_missed is actually "inferred" ("before next Friday" isn't a gated keyword
    # phrase, so the relative-weekday parser never sees it) but gets mis-predicted
    # "no_deadline" (a false positive for no_deadline, a false negative for inferred).
    assert no_deadline.tp == 2  # fp_trap, tn_chatter
    assert no_deadline.fp == 1  # fn_missed wrongly predicted no_deadline
    assert no_deadline.fn == 0

    assert explicit.tp == 1  # tp_explicit
    assert explicit.fp == 0
    assert explicit.fn == 0

    assert inferred.tp == 0
    assert inferred.fp == 0
    assert inferred.fn == 1  # fn_missed actually inferred but predicted no_deadline

    assert no_deadline.precision == 2 / 3
    assert no_deadline.recall == 1.0
    assert explicit.precision == 1.0
    assert explicit.recall == 1.0
    assert inferred.precision is None  # 0/0: no inferred predictions were made at all
    assert inferred.recall == 0.0

    assert report.overall_accuracy == 0.75
    assert report.priority_status == PRIORITY_NOT_EVALUATED


def test_evaluate_due_at_and_divergence_checks():
    diverging = _event(
        "div",
        structured={"due_at": "2026-07-10T00:00:00+00:00"},
        summary="but the real deadline is due 2026-07-20",
    )
    pairs = [
        (
            diverging,
            GoldLabel(
                dedupe_id="div",
                has_deadline=True,
                origin="explicit",
                due_at=datetime(2026, 7, 10, tzinfo=UTC),
                divergence_expected=True,
            ),
        )
    ]
    report = evaluate(pairs, RuleDeadlineExtractor())
    assert report.due_at_agreement_rate == 1.0
    assert report.divergence_recall == 1.0


def test_calibration_buckets_group_by_confidence():
    tp_explicit = _event("tp", structured={"due_at": "2026-07-10T00:00:00+00:00"})
    tn_chatter = _event("tn", summary="just checking in")

    pairs = [
        (tp_explicit, GoldLabel(dedupe_id="tp", has_deadline=True, origin="explicit")),
        (tn_chatter, GoldLabel(dedupe_id="tn", has_deadline=False)),
    ]
    report = evaluate(pairs, RuleDeadlineExtractor())
    labels = {b.label: b for b in report.calibration_buckets}
    assert labels["0.9-1.0 (explicit/source)"].n == 1
    assert labels["0.9-1.0 (explicit/source)"].accuracy == 1.0
    assert labels["0.0 (no candidate)"].n == 1
    assert labels["0.0 (no candidate)"].accuracy == 1.0


# --------------------------------------------------------------------------- #
# Go/no-go threshold rule
# --------------------------------------------------------------------------- #


def test_go_no_go_refuses_small_samples():
    tiny_report = evaluate(
        [(_event("a", summary="no signal"), GoldLabel(dedupe_id="a", has_deadline=False))],
        RuleDeadlineExtractor(),
    )
    decision = evaluate_go_no_go(tiny_report)
    assert decision.verdict == "insufficient_data"


def test_go_no_go_go_and_no_go_paths():
    from cadence.spikes.s0_2.calibration import CalibrationReport, ClassCounts

    good = CalibrationReport(
        n_events=500,
        overall_accuracy=0.95,
        per_class={
            "no_deadline": ClassCounts(tp=300, fp=5, fn=5),
            "explicit": ClassCounts(tp=100, fp=2, fn=3),
            "inferred": ClassCounts(tp=80, fp=5, fn=10),
        },
        calibration_buckets=[],
        due_at_agreement_rate=1.0,
        divergence_recall=1.0,
    )
    thresholds = GoNoGoThresholds(precision_floor=0.9, recall_floor=0.7, min_shadow_events=200)
    decision = evaluate_go_no_go(good, thresholds)
    assert decision.verdict == "go"

    bad = CalibrationReport(
        n_events=500,
        overall_accuracy=0.5,
        per_class={
            "no_deadline": ClassCounts(tp=100, fp=100, fn=100),
            "explicit": ClassCounts(tp=10, fp=50, fn=50),
            "inferred": ClassCounts(tp=5, fp=50, fn=50),
        },
        calibration_buckets=[],
        due_at_agreement_rate=None,
        divergence_recall=None,
    )
    decision = evaluate_go_no_go(bad, thresholds)
    assert decision.verdict == "no_go"
    assert decision.reasons


# --------------------------------------------------------------------------- #
# End-to-end: S0.0 synthetic sample -> S0.2 harness
# --------------------------------------------------------------------------- #


def test_end_to_end_on_s0_0_synthetic_sample_proves_harness_not_a_verdict(settings):
    nas = NASStore(settings)
    signal_log, label_store = build_bootstrap_sample(nas)
    sample = label_store.as_labeled_sample(signal_log)

    report = evaluate(sample.pairs(), RuleDeadlineExtractor())
    assert report.n_events == 12
    assert 0.0 <= report.overall_accuracy <= 1.0
    assert report.priority_status == PRIORITY_NOT_EVALUATED

    # The synthetic sample is far too small to back a real promotion decision --
    # the harness must say so rather than fabricate a "go".
    decision = evaluate_go_no_go(report)
    assert decision.verdict == "insufficient_data"
