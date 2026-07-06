"""Shadow -> live go/no-go threshold rule (spike S0.2, backfills AC-3).

:class:`GoNoGoThresholds` are STARTING DEFAULT floors informed by this harness's design
(and by running it against the small S0.0 synthetic sample to sanity-check the
arithmetic) — they are NOT values fit to real data. Recalibrate against a real captured
signal log (S0.0 run against live adapters, not fixtures) before trusting a "go"
verdict; see ``.omc/research/spikes/s0_2.md``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .calibration import CalibrationReport


@dataclass(frozen=True)
class GoNoGoThresholds:
    """Proposed shadow -> live promotion floors for the deadline/priority extractor."""

    precision_floor: float = 0.90
    recall_floor: float = 0.70
    #: Placeholder budget for a future priority extractor's false-positive rate -- not
    #: enforced yet since no priority extractor exists (see
    #: ``calibration.PRIORITY_NOT_EVALUATED``).
    false_priority_budget: float = 0.05
    #: Minimum shadow-period sample size before a promotion decision is trusted at all.
    min_shadow_events: int = 200
    evaluated_classes: tuple[str, ...] = ("explicit", "inferred")


@dataclass
class GoNoGoDecision:
    verdict: str  # "go" | "no_go" | "insufficient_data"
    reasons: list[str] = field(default_factory=list)


def evaluate_go_no_go(
    report: CalibrationReport, thresholds: GoNoGoThresholds | None = None
) -> GoNoGoDecision:
    """Apply the shadow->live promotion rule to a calibration report.

    Refuses to render a "go"/"no_go" opinion below ``min_shadow_events`` — a small
    sample (like the synthetic S0.0 set this harness self-tests against) can only prove
    the *arithmetic* is right, not that the extractor is ready for live traffic.
    """
    thresholds = thresholds or GoNoGoThresholds()

    if report.n_events < thresholds.min_shadow_events:
        return GoNoGoDecision(
            verdict="insufficient_data",
            reasons=[
                f"n_events={report.n_events} < min_shadow_events="
                f"{thresholds.min_shadow_events}; no real verdict possible on this sample"
            ],
        )

    reasons: list[str] = []
    ok = True
    for cls in thresholds.evaluated_classes:
        counts = report.per_class[cls]
        precision = counts.precision
        recall = counts.recall
        if precision is None or precision < thresholds.precision_floor:
            ok = False
            reasons.append(f"{cls}: precision={precision} < floor {thresholds.precision_floor}")
        if recall is None or recall < thresholds.recall_floor:
            ok = False
            reasons.append(f"{cls}: recall={recall} < floor {thresholds.recall_floor}")
    if ok:
        reasons.append("all evaluated classes meet precision/recall floors")
    return GoNoGoDecision(verdict="go" if ok else "no_go", reasons=reasons)


__all__ = ["GoNoGoThresholds", "GoNoGoDecision", "evaluate_go_no_go"]
