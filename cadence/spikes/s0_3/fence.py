"""Fence token + idempotent-nudge boundary (Decision C split-brain control).

Two guards, layered, sit between any leader and the device:

1. **Fence token** — every nudge write carries the leader's ``epoch``. The dispatcher
   remembers the highest epoch it has ever accepted (``max_epoch``). A write whose epoch is
   *below* ``max_epoch`` is **rejected/fenced** — this is the classic fencing-token pattern
   (Kleppmann). It means the instant a new leader (higher epoch) dispatches anything, the
   old leader's writes stop being accepted, even if the old leader still *believes* it leads.

2. **Idempotent nudge IDs + device dedupe** — a nudge's id is a pure function of its logical
   content (source event + kind), **not** random and **not** tied to which leader produced
   it. So the same logical nudge minted by a recovering old leader and by the new leader
   collapses to one id, and the device delivers each id at most once (exactly-once).

The two guards are independent on purpose: fencing stops the stale *writer*, dedupe stops
the double *delivery*. Either alone would prevent a double-nudge; together they are
defense-in-depth for the failover window.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field


def make_nudge_id(source_event_id: str, kind: str = "nudge") -> str:
    """Deterministic, leader-independent nudge id.

    Derived only from *logical* content so two leaders that both react to the same event
    produce the **same** id — the precondition for cross-leader dedupe.
    """
    digest = hashlib.sha256(f"{kind}:{source_event_id}".encode()).hexdigest()
    return f"ndg_{digest[:16]}"


@dataclass(frozen=True)
class Nudge:
    """A nudge write. ``epoch`` is the fence token of the leader that emitted it."""

    nudge_id: str
    source_event_id: str
    epoch: int
    emitted_by: str


@dataclass
class DispatchResult:
    accepted: bool
    reason: str  # "delivered" | "duplicate" | "fenced"


@dataclass
class FencedDispatcher:
    """The write boundary at the device edge: fences stale epochs, dedupes nudge ids.

    Records enough history for the chaos harness to assert on: what was delivered, what was
    fenced, and how many times each nudge id actually reached the device.
    """

    max_epoch: int = 0
    _delivered: dict[str, Nudge] = field(default_factory=dict)
    delivery_counts: dict[str, int] = field(default_factory=dict)
    fenced: list[Nudge] = field(default_factory=list)
    deduped: int = 0  # duplicate attempts the guard blocked (evidence it fired)

    def dispatch(self, nudge: Nudge) -> DispatchResult:
        # Guard 1: fence token. A stale (lower-epoch) leader is rejected outright.
        if nudge.epoch < self.max_epoch:
            self.fenced.append(nudge)
            return DispatchResult(False, "fenced")
        # A valid (>=) epoch advances the fence, deposing every lower epoch from now on.
        self.max_epoch = nudge.epoch
        # Guard 2: idempotent delivery. Same logical id -> delivered at most once.
        if nudge.nudge_id in self._delivered:
            self.deduped += 1  # a would-be double, blocked before it reaches the device
            return DispatchResult(False, "duplicate")
        self._delivered[nudge.nudge_id] = nudge
        self.delivery_counts[nudge.nudge_id] = 1
        return DispatchResult(True, "delivered")

    @property
    def delivered_ids(self) -> set[str]:
        return set(self._delivered)

    def double_delivery_count(self) -> int:
        """Number of nudge ids that actually reached the device more than once (must be 0)."""
        return sum(count - 1 for count in self.delivery_counts.values() if count > 1)
