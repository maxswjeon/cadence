"""Place/fusion co-presence simulation (S0.4a — RUNNABLE on synthetic data).

Combines three **context-prior** signals about the owner's phone — geofence
same-place membership, calendar-attendee overlap, and BLE proximity to a
*known, previously paired* contact device — into a single co-presence
**likelihood** score. Per ``.omc/research/s24-sensors-and-vad-device.md``, none
of these signals can identify a non-cooperating bystander (BLE MAC
randomization defeats passive person-ID; geofence/calendar only place the
owner, not who else is there); they are context priors that raise or lower
the odds that a *specific, already-known* person is co-present, nothing more.

This module is deliberately audio-free and dependency-free (stdlib only): it
generates a **synthetic** labeled dataset, fuses signals per sample, and
computes a real ROC curve + AUC on that synthetic data to prove the fusion
harness works end to end. The ROC numbers below are a harness demonstration,
**not** a measurement of real-world co-presence accuracy — that requires real
place/calendar/BLE logs, which this spike environment does not have.

Fusion method
--------------
Each signal is turned into a confidence in ``[0, 1]``, then combined with
**weighted log-odds pooling** (a naive-Bayes-style independent-evidence
combiner): a confidence is converted to log-odds, scaled by a per-signal
weight, summed, and squashed back through a sigmoid. This lets one strong
signal (e.g. same room *and* on the calendar) dominate two weak/absent ones,
unlike a plain average which would dilute it.
"""

from __future__ import annotations

import math
import random
from collections.abc import Sequence
from dataclasses import dataclass, field

#: Confidences are clamped away from exactly 0/1 before taking log-odds, since
#: logit(0) and logit(1) are undefined (would blow up the fused score to +-inf).
_EPS = 1e-3


def _logit(p: float) -> float:
    p = min(max(p, _EPS), 1.0 - _EPS)
    return math.log(p / (1.0 - p))


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


# --------------------------------------------------------------------------- #
# Signals
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class GeofenceSignal:
    """Same-place membership from the owner's own phone (GPS/Wi-Fi place-id).

    Context prior only: it says the owner's phone is at a place, not who else
    is there. ``distance_m`` is the owner's distance from the place centroid
    when ``same_place`` is true (irrelevant otherwise).
    """

    same_place: bool
    distance_m: float = 0.0


@dataclass(frozen=True)
class CalendarSignal:
    """Whether the other (known) person is a listed attendee of the owner's
    current calendar event, and how much of the event window is active now."""

    is_attendee: bool
    overlap_frac: float = 0.0  # 0..1 fraction of the event window active "now"


@dataclass(frozen=True)
class BluetoothSignal:
    """BLE RSSI proximity to a KNOWN, previously-paired contact device.

    Not passive bystander detection: BLE MAC randomization (~15 min rotation)
    makes re-identifying a stranger's phone infeasible, so this signal is only
    meaningful when the other person is an already-known contact whose device
    has previously been paired/enrolled with Cadence. ``rssi_dbm=None`` means
    no such device was observed.
    """

    rssi_dbm: float | None = None


@dataclass(frozen=True)
class CoPresenceSignals:
    """The three fused inputs for one co-presence likelihood estimate."""

    geofence: GeofenceSignal
    calendar: CalendarSignal
    bluetooth: BluetoothSignal


@dataclass(frozen=True)
class FusionWeights:
    """Per-signal log-odds weights. Higher = that signal moves the fused score more."""

    geofence: float = 1.0
    calendar: float = 1.2
    bluetooth: float = 0.8


def geofence_confidence(sig: GeofenceSignal, *, radius_m: float = 50.0) -> float:
    if not sig.same_place:
        return 0.0
    if radius_m <= 0:
        return 1.0
    return max(0.0, 1.0 - sig.distance_m / radius_m)


def calendar_confidence(sig: CalendarSignal) -> float:
    if not sig.is_attendee:
        return 0.0
    return max(0.0, min(1.0, sig.overlap_frac))


def bluetooth_confidence(
    sig: BluetoothSignal, *, rssi_strong: float = -60.0, rssi_weak: float = -90.0
) -> float:
    """Map RSSI (dBm) to a confidence via linear interpolation between two thresholds."""
    if sig.rssi_dbm is None:
        return 0.0
    if sig.rssi_dbm >= rssi_strong:
        return 1.0
    if sig.rssi_dbm <= rssi_weak:
        return 0.0
    return (sig.rssi_dbm - rssi_weak) / (rssi_strong - rssi_weak)


def fuse_signals(signals: CoPresenceSignals, weights: FusionWeights = FusionWeights()) -> float:
    """Fuse the three signals into one co-presence likelihood in ``[0, 1]``."""
    g = geofence_confidence(signals.geofence)
    c = calendar_confidence(signals.calendar)
    b = bluetooth_confidence(signals.bluetooth)
    log_odds = (
        weights.geofence * _logit(g) + weights.calendar * _logit(c) + weights.bluetooth * _logit(b)
    )
    return _sigmoid(log_odds)


# --------------------------------------------------------------------------- #
# Synthetic labeled dataset (RUNNABLE — clearly synthetic, not real evidence)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class LabeledSample:
    """One synthetic sample: fused-input signals + a synthetic ground-truth label."""

    signals: CoPresenceSignals
    co_present: bool


def generate_synthetic_dataset(n: int = 500, *, seed: int = 0) -> list[LabeledSample]:
    """Generate a synthetic, noisily-separable co-presence dataset.

    Exactly half the samples are labeled co-present; each signal is drawn from
    a distribution conditioned on that label with deliberate overlap, so the
    resulting ROC is neither trivially perfect nor useless — it approximates a
    plausible (not measured) real-sensor separability. **Synthetic only** —
    this proves the fusion+ROC harness runs correctly; it is not an empirical
    accuracy claim about real place/calendar/BLE data.
    """
    rng = random.Random(seed)
    samples: list[LabeledSample] = []
    for i in range(n):
        co_present = i % 2 == 0
        samples.append(LabeledSample(signals=_draw_signals(rng, co_present), co_present=co_present))
    rng.shuffle(samples)
    return samples


def _draw_signals(rng: random.Random, co_present: bool) -> CoPresenceSignals:
    if co_present:
        same_place = rng.random() < 0.80
        distance_m = rng.uniform(0, 20) if same_place else rng.uniform(0, 200)
        is_attendee = rng.random() < 0.70
        overlap_frac = rng.uniform(0.5, 1.0) if is_attendee else 0.0
        has_bt = rng.random() < 0.55
        rssi = rng.uniform(-75, -45) if has_bt else None
    else:
        same_place = rng.random() < 0.20
        distance_m = rng.uniform(0, 200)
        is_attendee = rng.random() < 0.15
        overlap_frac = rng.uniform(0.0, 0.5) if is_attendee else 0.0
        has_bt = rng.random() < 0.10
        rssi = rng.uniform(-95, -75) if has_bt else None

    return CoPresenceSignals(
        geofence=GeofenceSignal(same_place=same_place, distance_m=distance_m),
        calendar=CalendarSignal(is_attendee=is_attendee, overlap_frac=overlap_frac),
        bluetooth=BluetoothSignal(rssi_dbm=rssi),
    )


# --------------------------------------------------------------------------- #
# ROC / AUC (RUNNABLE — real numbers on the synthetic dataset above)
# --------------------------------------------------------------------------- #


@dataclass
class RocResult:
    """A discrete ROC curve (including the ``(0,0)`` and ``(1,1)`` endpoints) + its AUC."""

    points: list[tuple[float, float]] = field(default_factory=list)  # (fpr, tpr)
    auc: float = 0.0
    n_pos: int = 0
    n_neg: int = 0


def compute_roc(
    samples: Sequence[LabeledSample], weights: FusionWeights = FusionWeights()
) -> RocResult:
    """Score every sample, then sweep the threshold from high to low score.

    Standard ROC construction: sort by descending fused score, walk the sorted
    list accumulating true/false positives, and record ``(fpr, tpr)`` after
    each sample. AUC is the trapezoidal area under the resulting curve.
    """
    scored = sorted(
        ((fuse_signals(s.signals, weights), s.co_present) for s in samples),
        key=lambda pair: pair[0],
        reverse=True,
    )
    n_pos = sum(1 for _, label in scored if label)
    n_neg = len(scored) - n_pos

    points: list[tuple[float, float]] = [(0.0, 0.0)]
    tp = fp = 0
    for _score, label in scored:
        if label:
            tp += 1
        else:
            fp += 1
        fpr = fp / n_neg if n_neg else 0.0
        tpr = tp / n_pos if n_pos else 0.0
        points.append((fpr, tpr))

    return RocResult(points=points, auc=_trapezoid_auc(points), n_pos=n_pos, n_neg=n_neg)


def _trapezoid_auc(points: list[tuple[float, float]]) -> float:
    ordered = sorted(points)
    area = 0.0
    for (x0, y0), (x1, y1) in zip(ordered, ordered[1:], strict=False):
        area += (x1 - x0) * (y0 + y1) / 2.0
    return area


__all__ = [
    "GeofenceSignal",
    "CalendarSignal",
    "BluetoothSignal",
    "CoPresenceSignals",
    "FusionWeights",
    "geofence_confidence",
    "calendar_confidence",
    "bluetooth_confidence",
    "fuse_signals",
    "LabeledSample",
    "generate_synthetic_dataset",
    "RocResult",
    "compute_roc",
]
