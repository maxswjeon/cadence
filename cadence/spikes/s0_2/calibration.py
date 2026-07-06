"""Inference-calibration harness (spike S0.2, backfills AC-3).

Evaluates the deadline extractor-under-test
(:class:`cadence.brain.deadlines.RuleDeadlineExtractor`; the LLM path is a documented
stub — no live LLM call here, see the ``llm_hook`` seam in that module) against a
labeled sample built by :mod:`cadence.spikes.s0_0`. Computes:

* a 3-class one-vs-rest confusion matrix (``no_deadline`` / ``explicit`` /
  ``inferred``) with per-class precision/recall,
* a confidence-bucketed calibration curve (observed accuracy per confidence bucket),
* due-date agreement and divergence-detection rates as auxiliary checks,

which feed :mod:`.thresholds`'s shadow->live go/no-go rule.

**Honest gating**: this module computes real arithmetic on whatever sample it's given.
It does not itself decide "the extractor is good enough" — that verdict requires a real
captured signal log, which does not exist yet. Running it on the small synthetic S0.0
sample (:mod:`.run`) proves the harness is correct; see ``.omc/research/spikes/s0_2.md``.
"""

from __future__ import annotations

from dataclasses import dataclass

from cadence.adapters.base import Event
from cadence.brain.deadlines import DeadlineCandidate, DeadlineExtractor
from cadence.spikes.s0_0.labeling import GoldLabel

_CLASSES = ("no_deadline", "explicit", "inferred")

#: No priority extractor exists anywhere in the codebase yet (`Task.priority` in
#: `cadence/stores/models.py` is a schema field only) — priority calibration is left as
#: this documented stub rather than a fabricated verdict. See s0_2.md.
PRIORITY_NOT_EVALUATED = (
    "not evaluated: no priority extractor exists in cadence.brain.* yet -- "
    "Task.priority (cadence/stores/models.py) is a schema field only, unimplemented "
    "for extraction. See .omc/research/spikes/s0_2.md."
)


@dataclass
class ClassCounts:
    """One-vs-rest tp/fp/fn counts for a single predicted/gold class."""

    tp: int = 0
    fp: int = 0
    fn: int = 0

    @property
    def precision(self) -> float | None:
        denom = self.tp + self.fp
        return self.tp / denom if denom else None

    @property
    def recall(self) -> float | None:
        denom = self.tp + self.fn
        return self.tp / denom if denom else None


@dataclass
class CalibrationBucket:
    """One confidence bucket's observed accuracy — a calibration-curve point."""

    label: str
    n: int
    accuracy: float | None


@dataclass
class CalibrationReport:
    """Output of :func:`evaluate` — the numbers that would back-fill AC-3."""

    n_events: int
    overall_accuracy: float
    per_class: dict[str, ClassCounts]
    calibration_buckets: list[CalibrationBucket]
    due_at_agreement_rate: float | None
    divergence_recall: float | None
    priority_status: str = PRIORITY_NOT_EVALUATED

    def precision(self, cls: str) -> float | None:
        return self.per_class[cls].precision

    def recall(self, cls: str) -> float | None:
        return self.per_class[cls].recall


def _predicted_class(candidates: list[DeadlineCandidate]) -> str:
    """Explicit-preferred predicted class, mirroring ``RuleDeadlineExtractor``'s own
    explicit-over-inferred convention (see ``_reconcile``)."""
    if not candidates:
        return "no_deadline"
    origins = {c.origin for c in candidates}
    return "explicit" if "explicit" in origins else "inferred"


def _gold_class(gold: GoldLabel) -> str:
    if not gold.has_deadline:
        return "no_deadline"
    return gold.origin or "inferred"


def _top_candidate(candidates: list[DeadlineCandidate]) -> DeadlineCandidate | None:
    """The explicit-preferred candidate (matches ``_reconcile``'s ordering: explicit
    first when present)."""
    for c in candidates:
        if c.origin == "explicit":
            return c
    return candidates[0] if candidates else None


def _confidence_bucket(confidence: float) -> str:
    if confidence <= 0.0:
        return "0.0 (no candidate)"
    if confidence < 0.9:
        return "0.5-0.89 (heuristic)"
    return "0.9-1.0 (explicit/source)"


def evaluate(
    pairs: list[tuple[Event, GoldLabel]], extractor: DeadlineExtractor
) -> CalibrationReport:
    """Run ``extractor`` over every (event, gold label) pair and compute metrics.

    Read-only over the extractor contract (no I/O): calls ``extractor.extract(event)``
    once per pair and compares the result against the gold label.
    """
    per_class: dict[str, ClassCounts] = {cls: ClassCounts() for cls in _CLASSES}
    bucket_hits: dict[str, list[bool]] = {}
    correct = 0
    due_at_checks: list[bool] = []
    divergence_checks: list[bool] = []

    for event, gold in pairs:
        candidates = extractor.extract(event)
        predicted = _predicted_class(candidates)
        actual = _gold_class(gold)

        if predicted == actual:
            per_class[actual].tp += 1
            correct += 1
        else:
            per_class[predicted].fp += 1
            per_class[actual].fn += 1

        top = _top_candidate(candidates)
        confidence = (top.confidence_value or 0.0) if top is not None else 0.0
        bucket_hits.setdefault(_confidence_bucket(confidence), []).append(predicted == actual)

        if (
            gold.has_deadline
            and predicted != "no_deadline"
            and gold.due_at is not None
            and top is not None
        ):
            due_at_checks.append(top.due_at.date() == gold.due_at.date())

        if gold.divergence_expected:
            divergence_checks.append(any(c.divergence_flag for c in candidates))

    buckets = [
        CalibrationBucket(label=label, n=len(hits), accuracy=sum(hits) / len(hits))
        for label, hits in sorted(bucket_hits.items())
    ]

    n = len(pairs)
    return CalibrationReport(
        n_events=n,
        overall_accuracy=(correct / n) if n else 0.0,
        per_class=per_class,
        calibration_buckets=buckets,
        due_at_agreement_rate=(sum(due_at_checks) / len(due_at_checks)) if due_at_checks else None,
        divergence_recall=(
            (sum(divergence_checks) / len(divergence_checks)) if divergence_checks else None
        ),
    )


__all__ = [
    "ClassCounts",
    "CalibrationBucket",
    "CalibrationReport",
    "evaluate",
    "PRIORITY_NOT_EVALUATED",
]
