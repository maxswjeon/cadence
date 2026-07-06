"""Chaos orchestrator for S0.3: Normal -> NAS-down -> cloud-takeover -> stale-recovery.

Drives the actors through the full failover transition and records a **timeline of
snapshots** after every step so the test can assert its safety properties *continuously*,
not just at the end. The properties (from AC-6 / Decision C):

* **exactly-one active leader at all times** — never two authoritative writers; after
  recovery there is a leader (liveness);
* **the fenced stale writer's writes are rejected** — the recovered self-host, still on its
  dead epoch, is refused at the dispatcher;
* **0 double-nudges** — idempotent dedupe holds across the leadership handoff;
* **0 lost buffered events** — the device edge buffer fully drains after recovery.

An "active leader" is a node that *believes* it leads **and** whose epoch matches the
independent authority's current epoch **and** holds a live lease. The stale self-host
believes it leads but fails all three of the latter, so it is correctly not counted.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .authority import LeaseStore, LogicalClock
from .cluster import BrainNode, Device
from .fence import FencedDispatcher


@dataclass
class Snapshot:
    """State captured after a single simulation step, for timeline assertions."""

    label: str
    tick: int
    active_leaders: list[str]
    store_epoch: int
    fence_epoch: int
    buffer_depth: int
    delivered_ids: frozenset[str]
    double_deliveries: int


@dataclass
class ChaosReport:
    timeline: list[Snapshot] = field(default_factory=list)
    captured_events: list[str] = field(default_factory=list)
    fenced_writes: int = 0
    stale_renew_rejected: bool = False

    @property
    def max_active_leaders(self) -> int:
        return max((len(s.active_leaders) for s in self.timeline), default=0)

    @property
    def double_nudges(self) -> int:
        return self.timeline[-1].double_deliveries if self.timeline else 0

    @property
    def lost_events(self) -> int:
        """Captured events whose nudge never reached the device (must be 0)."""
        delivered = self.timeline[-1].delivered_ids if self.timeline else frozenset()
        from .fence import make_nudge_id

        return sum(1 for ev in self.captured_events if make_nudge_id(ev) not in delivered)

    @property
    def final_buffer_depth(self) -> int:
        return self.timeline[-1].buffer_depth if self.timeline else 0


class ChaosSimulator:
    """Runs the failover scenario and returns a :class:`ChaosReport`.

    ``lease_ttl`` is in clock ticks; the scenario advances the clock by hand so the whole
    run is deterministic and reproducible.
    """

    def __init__(self, lease_ttl: int = 10) -> None:
        self.clock = LogicalClock()
        self.store = LeaseStore(ttl=lease_ttl)
        self.dispatcher = FencedDispatcher()
        self.selfhost = BrainNode("self-host")
        self.cloud = BrainNode("cloud")
        self.device = Device("phone")
        self.report = ChaosReport()

    # -- observation -------------------------------------------------------------------
    def _active_leaders(self) -> list[str]:
        now = self.clock.now()
        live = self.store.live_holder(now)
        active: list[str] = []
        for node in (self.selfhost, self.cloud):
            if node.believes_leader() and node.epoch == self.store.epoch and live == node.node_id:
                active.append(node.node_id)
        return active

    def _snapshot(self, label: str) -> None:
        self.report.timeline.append(
            Snapshot(
                label=label,
                tick=self.clock.now(),
                active_leaders=self._active_leaders(),
                store_epoch=self.store.epoch,
                fence_epoch=self.dispatcher.max_epoch,
                buffer_depth=len(self.device.buffer),
                delivered_ids=frozenset(self.dispatcher.delivered_ids),
                double_deliveries=self.dispatcher.double_delivery_count(),
            )
        )

    # -- scenario ----------------------------------------------------------------------
    def run(self) -> ChaosReport:
        # === Phase 1: Normal — self-host is the sole leader ===========================
        self.selfhost.acquire(self.store, self.clock.now())  # epoch 1
        self._snapshot("normal:selfhost-leader")

        # Device captures and the leader nudges each event; delivered exactly once.
        for ev in ("e1", "e2"):
            self.device.capture(ev)
        self.report.captured_events.extend(("e1", "e2"))
        self.device.drain_to(self.selfhost, self.dispatcher)
        self._snapshot("normal:e1-e2-nudged")

        # An event is captured and buffered but NOT yet processed by the self-host.
        self.device.capture("e3")
        self.report.captured_events.append("e3")
        self._snapshot("normal:e3-buffered-inflight")

        # === Phase 2: Degraded — NAS/self-host dies mid-flight ========================
        self.selfhost.crash()  # goes dark; stops renewing its lease
        self.clock.advance(11)  # push past the lease TTL so the lease lapses
        self._snapshot("degraded:selfhost-down-lease-lapsed")

        # Cloud takes leadership — a NEW term, higher epoch (the fence advances).
        assert self.cloud.acquire(self.store, self.clock.now()), "cloud must win the lapsed lease"
        self._snapshot("degraded:cloud-leader-epoch2")

        # Device reconnects to the cloud and drains its edge buffer (e3) to it.
        self.device.drain_to(self.cloud, self.dispatcher)
        self._snapshot("degraded:buffer-drained-to-cloud")

        # === Phase 3: Recovery — self-host returns as a STALE leader ==================
        self.selfhost.recover_stale()  # alive again, still believing epoch 1
        self._snapshot("recovery:stale-selfhost-alive")

        # Stale self-host re-processes e3 (the write it never finished) and dispatches it
        # with its DEAD epoch 1: a split-brain double-nudge attempt AND a stale write.
        result = self.selfhost.emit_nudge("e3", self.dispatcher)
        self.report.fenced_writes = len(self.dispatcher.fenced)
        assert result is not None and result.reason == "fenced", "stale write must be fenced"
        self._snapshot("recovery:stale-write-fenced")

        # Stale self-host tries to renew its term and is rejected by the authority -> steps down.
        self.report.stale_renew_rejected = not self.selfhost.renew(self.store, self.clock.now())
        self._snapshot("recovery:stale-renew-rejected")

        return self.report
