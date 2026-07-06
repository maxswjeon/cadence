"""The cluster actors: brain leader-candidates and edge-buffering devices.

* :class:`BrainNode` — a leader candidate (self-host or cloud). It only emits nudges while
  it believes it holds the lease, stamping each with its epoch (the fence token). A crashed
  node stops renewing; a recovered-but-stale node keeps its *old* epoch until it discovers
  it was deposed.
* :class:`Device` — captures events into an **edge buffer** while the brain is unreachable,
  then drains that buffer to whichever brain is currently leader. It delivers nudges through
  the :class:`~cadence.spikes.s0_3.fence.FencedDispatcher`, so double-delivery is dropped.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .authority import LeaseError, LeaseGrant, LeaseStore
from .fence import FencedDispatcher, Nudge, make_nudge_id


@dataclass
class BrainNode:
    """A brain instance that may hold leadership and emit nudges.

    ``grant`` is what the node *believes* about its leadership. After a stale recovery this
    can lag reality — the fence at the dispatcher and the epoch check at the lease store are
    what reconcile that belief with the truth.
    """

    node_id: str
    alive: bool = True
    grant: LeaseGrant | None = None

    def believes_leader(self) -> bool:
        return self.alive and self.grant is not None

    @property
    def epoch(self) -> int | None:
        return self.grant.epoch if self.grant is not None else None

    def acquire(self, store: LeaseStore, now: int) -> bool:
        """Try to become leader via the independent authority."""
        if not self.alive:
            return False
        grant = store.try_acquire(self.node_id, now)
        if grant is not None:
            self.grant = grant
            return True
        return False

    def renew(self, store: LeaseStore, now: int) -> bool:
        """Renew the owned term; on failure the node discovers it was deposed and steps down."""
        if not self.alive or self.grant is None:
            return False
        try:
            self.grant = store.renew(self.node_id, self.grant.epoch, now)
            return True
        except LeaseError:
            self.grant = None  # deposed: stop believing we lead
            return False

    def crash(self) -> None:
        """Self-host/NAS death: the node goes dark and stops renewing (lease will lapse)."""
        self.alive = False

    def recover_stale(self) -> None:
        """Come back believing the *old* term is still ours (the split-brain hazard).

        Note we keep ``self.grant`` (old epoch) untouched — that is exactly the stale-leader
        condition the fence must catch.
        """
        self.alive = True

    def emit_nudge(self, source_event_id: str, dispatcher: FencedDispatcher):
        """Emit a nudge stamped with our believed epoch. Returns the dispatch result.

        No-op guard: a node that does not believe it leads emits nothing. The interesting
        case is the stale leader that *does* still believe it leads with a dead epoch — its
        write reaches the dispatcher and is fenced there.
        """
        if not self.believes_leader():
            return None
        nudge = Nudge(
            nudge_id=make_nudge_id(source_event_id),
            source_event_id=source_event_id,
            epoch=self.grant.epoch,
            emitted_by=self.node_id,
        )
        return dispatcher.dispatch(nudge)


@dataclass
class Device:
    """An edge device: buffers captured events, drains them to the current leader.

    ``captured`` is every event id the device ever saw (ground truth for the "0 lost
    buffered events" assertion). ``buffer`` holds events not yet acked by a leader.
    """

    device_id: str
    captured: list[str] = field(default_factory=list)
    buffer: list[str] = field(default_factory=list)

    def capture(self, event_id: str) -> None:
        self.captured.append(event_id)
        self.buffer.append(event_id)

    def drain_to(self, leader: BrainNode, dispatcher: FencedDispatcher) -> list[str]:
        """Flush the edge buffer to ``leader``, which emits a nudge per event.

        An event is only removed from the buffer once the leader has *accepted* its nudge
        (delivered or deduped — both mean it was durably handled). A fenced write leaves the
        event in the buffer to be retried against the real leader, so nothing is lost.
        """
        drained: list[str] = []
        remaining: list[str] = []
        for event_id in self.buffer:
            result = leader.emit_nudge(event_id, dispatcher)
            if result is not None and result.reason in ("delivered", "duplicate"):
                drained.append(event_id)
            else:
                remaining.append(event_id)  # fenced / no leader -> keep buffered
        self.buffer = remaining
        return drained
