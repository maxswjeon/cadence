"""End-to-end demo: build the S0.0 bootstrap sample, run the S0.2 calibration harness
against it, and print the resulting report + go/no-go verdict.

Run directly: ``python -m cadence.spikes.s0_2.run``. :func:`run_demo` is what
``tests/test_spike_s0_2.py``'s end-to-end test also calls. See
``.omc/research/spikes/s0_2.md`` for what the printed numbers do (and do not) prove —
in short: the harness's arithmetic, not a real "ship it" verdict.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from cadence.brain.deadlines import RuleDeadlineExtractor
from cadence.spikes.s0_0.sample import build_bootstrap_sample
from cadence.stores.nas import NASStore

from .calibration import CalibrationReport, evaluate
from .thresholds import GoNoGoDecision, evaluate_go_no_go


def run_demo(nas: NASStore | None = None) -> tuple[CalibrationReport, GoNoGoDecision]:
    """Build the synthetic S0.0 sample and run the S0.2 calibration harness on it."""
    if nas is None:
        nas = NASStore(base_dir=Path(tempfile.mkdtemp(prefix="cadence-s0-2-")))
    signal_log, label_store = build_bootstrap_sample(nas)
    sample = label_store.as_labeled_sample(signal_log)
    report = evaluate(sample.pairs(), RuleDeadlineExtractor())
    decision = evaluate_go_no_go(report)
    return report, decision


def _print_report(report: CalibrationReport, decision: GoNoGoDecision) -> None:
    print(f"n_events={report.n_events} overall_accuracy={report.overall_accuracy:.2f}")
    for cls, counts in report.per_class.items():
        print(
            f"  {cls}: tp={counts.tp} fp={counts.fp} fn={counts.fn} "
            f"precision={counts.precision} recall={counts.recall}"
        )
    for bucket in report.calibration_buckets:
        print(f"  bucket {bucket.label}: n={bucket.n} accuracy={bucket.accuracy}")
    print(f"due_at_agreement_rate={report.due_at_agreement_rate}")
    print(f"divergence_recall={report.divergence_recall}")
    print(f"priority_status={report.priority_status}")
    print(f"go/no-go verdict={decision.verdict} reasons={decision.reasons}")


if __name__ == "__main__":
    _report, _decision = run_demo()
    _print_report(_report, _decision)


__all__ = ["run_demo"]
