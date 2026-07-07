"""Attention-state detector (engine Component 1).

Classifies the user's *current* attention state from recent device-activity
:class:`~cadence.stores.models.Fact` rows over a sliding window, with an **injected
clock** so the whole thing is deterministic and unit-testable.

Activity facts
--------------
The detector consumes facts whose ``kind`` is a device-activity kind
(:data:`AttentionConfig.activity_kinds`, e.g. ``device.app_usage``,
``device.active_window``, ``device.screen_context``) or any browser kind
(``kind`` starting with :data:`AttentionConfig.activity_prefixes`). Each such fact is
one *observation*:

* its **target** — what the user was on — is read from ``object_label`` (falling back
  to ``predicate`` then ``subject_id`` then ``kind``), and
* its **observation time** is the fact's ``created_at`` (for device telemetry, ingest
  time ≈ capture time).

No verbatim raw content is read — only structured provenance columns, upholding the D1
raw boundary. If ``persist=True`` the snapshot is written back as a single
``attention.state`` fact (structured/summary only, provenance carried).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum

from sqlalchemy import select

from cadence.brain.facts import FactGraph, FactInput
from cadence.stores.d1 import D1Store
from cadence.stores.models import Fact


class AttentionState(StrEnum):
    """The user's current attention state."""

    IDLE = "idle"            # no meaningful activity, or "what-next" limbo (rapid flailing)
    IMMERSED = "immersed"    # sustained focus on one app/context
    SCATTERED = "scattered"  # frequent context switching across real work


@dataclass(frozen=True)
class AttentionConfig:
    """Thresholds for attention classification (all seconds unless noted)."""

    #: Look-back window: only activity newer than ``now - window_seconds`` is considered.
    window_seconds: int = 1800
    #: Continuous dwell on one target at/above which the state is IMMERSED.
    immersion_seconds: int = 600
    #: If the newest activity is older than this, the user is IDLE (no recent activity).
    idle_gap_seconds: int = 300
    #: In "what-next limbo", nothing is held even this long — pure flailing → IDLE.
    limbo_dwell_seconds: int = 90
    #: Number of target switches in-window at/above which focus is fragmented.
    scatter_switches: int = 4
    #: Two consecutive same-target samples more than this far apart do NOT count as one
    #: continuous run (guards against two glances at the same app minutes apart reading as
    #: sustained immersion); each inter-sample interval is also capped at this when
    #: accumulating dwell.
    max_intra_run_gap_seconds: int = 300
    #: Activity fact kinds (exact match).
    activity_kinds: frozenset[str] = frozenset(
        {"device.app_usage", "device.active_window", "device.screen_context"}
    )
    #: Activity fact kind prefixes (browser/web activity).
    activity_prefixes: tuple[str, ...] = ("browser", "device.browser", "web.")

    def is_activity_kind(self, kind: str) -> bool:
        return kind in self.activity_kinds or kind.startswith(self.activity_prefixes)


@dataclass(frozen=True)
class AttentionSnapshot:
    """Immutable value object describing one attention classification."""

    state: AttentionState
    now: datetime
    focus_target: str | None = None
    focus_duration: timedelta | None = None
    confidence: float = 0.0
    #: Fact ids that were the evidence for this classification.
    evidence_fact_ids: list[str] = field(default_factory=list)
    #: Short human-readable reason (non-verbatim), useful for nudges/telemetry.
    reason: str = ""


@dataclass
class _Sample:
    fact_id: str
    target: str
    ts: datetime


@dataclass
class _Run:
    target: str
    #: Accumulated continuous dwell (sum of gap-capped inter-sample intervals).
    dwell: float


def _target_of(fact: Fact) -> str:
    return fact.object_label or fact.predicate or fact.subject_id or fact.kind


class AttentionDetector:
    """Classifies :class:`AttentionState` from recent activity facts.

    Two entry points:

    * :meth:`snapshot` — reads activity facts from D1 for ``now`` and classifies.
    * :meth:`classify` — the pure classification over an explicit sample list (used by
      tests and by :meth:`snapshot`); no I/O.
    """

    def __init__(
        self,
        d1: D1Store,
        config: AttentionConfig | None = None,
        *,
        facts: FactGraph | None = None,
    ) -> None:
        self.d1 = d1
        self.config = config or AttentionConfig()
        self._facts = facts

    # -- reads -------------------------------------------------------------- #

    def _load_samples(self, now: datetime) -> list[_Sample]:
        window_start = now - timedelta(seconds=self.config.window_seconds)
        with self.d1.session() as session:
            rows = session.execute(
                select(Fact)
                .where(Fact.created_at >= window_start)
                .where(Fact.created_at <= now)
                .order_by(Fact.created_at)
            ).scalars().all()
            samples = [
                _Sample(fact_id=f.id, target=_target_of(f), ts=f.created_at)
                for f in rows
                if self.config.is_activity_kind(f.kind)
            ]
        return samples

    def snapshot(self, now: datetime, *, persist: bool = False) -> AttentionSnapshot:
        """Classify the current attention state; optionally persist an ``attention.state`` fact."""
        snap = self.classify(now, self._load_samples(now))
        if persist:
            self.persist(snap)
        return snap

    # -- pure classification ------------------------------------------------ #

    def classify(self, now: datetime, samples: list[_Sample]) -> AttentionSnapshot:
        """Pure classifier over time-ordered activity samples (no I/O)."""
        cfg = self.config
        samples = sorted(samples, key=lambda s: s.ts)
        if not samples:
            return AttentionSnapshot(
                state=AttentionState.IDLE, now=now, confidence=0.7,
                reason="no activity in window",
            )

        last_gap = (now - samples[-1].ts).total_seconds()
        if last_gap > cfg.idle_gap_seconds:
            return AttentionSnapshot(
                state=AttentionState.IDLE, now=now, confidence=0.8,
                reason=f"no activity for {int(last_gap)}s",
                evidence_fact_ids=[s.fact_id for s in samples],
            )

        runs = self._runs(samples, now)
        current = runs[-1]
        current_dwell = current.dwell
        max_dwell = max(r.dwell for r in runs)
        switch_count = len(runs) - 1
        distinct = len({s.target for s in samples})
        evidence = [s.fact_id for s in samples]

        if current_dwell >= cfg.immersion_seconds:
            conf = 0.9 if current_dwell >= 1.5 * cfg.immersion_seconds else 0.78
            return AttentionSnapshot(
                state=AttentionState.IMMERSED, now=now, focus_target=current.target,
                focus_duration=timedelta(seconds=current_dwell), confidence=conf,
                evidence_fact_ids=evidence,
                reason=f"sustained focus on '{current.target}' for {int(current_dwell)}s",
            )

        if switch_count >= cfg.scatter_switches and max_dwell < cfg.limbo_dwell_seconds:
            return AttentionSnapshot(
                state=AttentionState.IDLE, now=now, focus_target=None,
                focus_duration=timedelta(seconds=current_dwell), confidence=0.6,
                evidence_fact_ids=evidence,
                reason=f"what-next limbo: {switch_count} switches, nothing held",
            )

        if switch_count >= cfg.scatter_switches:
            return AttentionSnapshot(
                state=AttentionState.SCATTERED, now=now, focus_target=current.target,
                focus_duration=timedelta(seconds=current_dwell), confidence=0.72,
                evidence_fact_ids=evidence,
                reason=f"{switch_count} context switches across {distinct} targets",
            )

        if distinct == 1:
            # Single target but dwell below the immersion threshold — the user IS on one
            # thing, but not long enough to call it real immersion. Report it as immersion
            # with confidence strictly below any firing threshold so it never triggers a
            # nudge on its own (genuine immersion is additionally gated on focus_duration
            # in the misallocation detector).
            return AttentionSnapshot(
                state=AttentionState.IMMERSED, now=now, focus_target=current.target,
                focus_duration=timedelta(seconds=current_dwell), confidence=0.2,
                evidence_fact_ids=evidence,
                reason=f"single focus '{current.target}' ({int(current_dwell)}s, below threshold)",
            )

        return AttentionSnapshot(
            state=AttentionState.SCATTERED, now=now, focus_target=current.target,
            focus_duration=timedelta(seconds=current_dwell), confidence=0.55,
            evidence_fact_ids=evidence,
            reason=f"{switch_count} switches across {distinct} targets (mild)",
        )

    def _runs(self, samples: list[_Sample], now: datetime) -> list[_Run]:
        """Group ordered samples into continuous-focus runs, accumulating dwell.

        A sample's attributed dwell is the interval to the next observation (``now`` for
        the last), **capped** at ``max_intra_run_gap_seconds`` — so a long silence between
        two same-target samples contributes at most that cap, not the whole gap. A gap
        wider than the cap also *splits* the run (it is no longer continuous focus).
        """
        max_gap = self.config.max_intra_run_gap_seconds
        n = len(samples)
        runs: list[_Run] = []
        for i, s in enumerate(samples):
            nxt = samples[i + 1].ts if i + 1 < n else now
            attributed = max(0.0, min((nxt - s.ts).total_seconds(), max_gap))
            gap_prev = (s.ts - samples[i - 1].ts).total_seconds() if i > 0 else 0.0
            if runs and runs[-1].target == s.target and gap_prev <= max_gap:
                runs[-1].dwell += attributed
            else:
                runs.append(_Run(target=s.target, dwell=attributed))
        return runs

    # -- persistence -------------------------------------------------------- #

    def persist(self, snap: AttentionSnapshot) -> Fact:
        """Persist a snapshot as a single ``attention.state`` fact (structured/summary only)."""
        facts = self._facts or FactGraph(self.d1)
        bucket = int(snap.now.timestamp()) // max(1, self.config.window_seconds)
        dwell = int(snap.focus_duration.total_seconds()) if snap.focus_duration else 0
        # Hash the basis so the (digit-heavy) dedupe key is a pure hex digest that the
        # raw-boundary classifier accepts, while staying stable per state+focus+window.
        basis = f"attention.state|{snap.state.value}|{snap.focus_target}|{bucket}"
        return facts.assert_fact(
            FactInput(
                kind="attention.state",
                subject_type="attention",
                subject_id="user",
                predicate="state",
                object_label=snap.state.value,
                confidence_value=snap.confidence,
                confidence_type="inferred",
                source_event_ids=list(snap.evidence_fact_ids),
                summary=f"attention={snap.state.value} focus={snap.focus_target} dwell={dwell}s",
                dedupe_key=hashlib.sha256(basis.encode("utf-8")).hexdigest(),
            )
        )


__all__ = [
    "AttentionState",
    "AttentionConfig",
    "AttentionSnapshot",
    "AttentionDetector",
]
