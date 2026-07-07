"""Attention engine entrypoint (engine Component 4).

:class:`AttentionEngine` is the periodic reasoning tick a scheduler would call. Each
:meth:`evaluate` reads the current D1 state, then runs the loop:

    attention (Component 1)
        → priority + misallocation (Component 2)
            → nudge governor (Component 3, shadow or live)

and returns an :class:`EngineTick` describing everything it saw and did. No live
scheduler or LLM is wired here — the periodic driver and the receptiveness/LLM
refinement are documented seams (see :mod:`cadence.engine.governor`).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from cadence.engine.attention import (
    AttentionConfig,
    AttentionDetector,
    AttentionSnapshot,
)
from cadence.engine.governor import NudgeGovernor, ProposedNudge
from cadence.engine.priority import (
    Misallocation,
    PriorityConfig,
    PriorityEngine,
    PriorityView,
)
from cadence.stores.d1 import D1Store


@dataclass(frozen=True)
class EngineTick:
    """The result of one :meth:`AttentionEngine.evaluate` call."""

    now: datetime
    snapshot: AttentionSnapshot
    priority_view: PriorityView
    misallocations: list[Misallocation] = field(default_factory=list)
    #: Nudges the governor emitted this tick (persisted in live mode, proposed in shadow).
    nudges: list[ProposedNudge] = field(default_factory=list)


class AttentionEngine:
    """Wires attention → priority → misallocation → governor into a periodic tick."""

    def __init__(
        self,
        d1: D1Store,
        governor: NudgeGovernor,
        *,
        clock: Callable[[], datetime] | None = None,
        detector: AttentionDetector | None = None,
        priority_engine: PriorityEngine | None = None,
        attention_config: AttentionConfig | None = None,
        priority_config: PriorityConfig | None = None,
        persist_attention: bool = False,
        evaluate_on_ingest: bool = False,
    ) -> None:
        self.d1 = d1
        self.governor = governor
        self.clock = clock
        self.detector = detector or AttentionDetector(d1, attention_config)
        self.priority = priority_engine or PriorityEngine(d1, priority_config)
        self.persist_attention = persist_attention
        #: Optional seam: allow high-signal ingest events to trigger an immediate
        #: evaluate. Off by default — the periodic tick is the primary driver.
        self.evaluate_on_ingest = evaluate_on_ingest

    def evaluate(self, now: datetime | None = None) -> EngineTick:
        """Run one reasoning tick for ``now`` (defaults to :attr:`clock` if provided)."""
        if now is None:
            if self.clock is None:
                raise ValueError("evaluate() needs an explicit `now` or a constructor `clock`")
            now = self.clock()

        snapshot = self.detector.snapshot(now, persist=self.persist_attention)
        view = self.priority.rank(now)
        # Only act on *genuine* immersion — a focus shorter than the immersion threshold is
        # not enough to nudge someone off it (guards against firing on a brief glance).
        misallocations = self.priority.detect_misallocation(
            snapshot, view, min_focus_seconds=self.detector.config.immersion_seconds
        )

        nudges: list[ProposedNudge] = []
        for misalloc in misallocations:
            emitted = self.governor.consider_misallocation(misalloc, snapshot, now)
            if emitted is not None:
                nudges.append(emitted)

        nudges.extend(self.governor.consider_device_care(now))

        return EngineTick(
            now=now,
            snapshot=snapshot,
            priority_view=view,
            misallocations=misallocations,
            nudges=nudges,
        )

    def on_ingest(self, now: datetime | None = None) -> EngineTick | None:
        """Optional ingest-triggered evaluation seam (no-op unless enabled)."""
        if not self.evaluate_on_ingest:
            return None
        return self.evaluate(now)


__all__ = ["AttentionEngine", "EngineTick"]
