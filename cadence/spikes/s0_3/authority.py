"""Single-leader lease/epoch authority for Cadence runtime failover (Decision C).

The **lease store is deliberately independent of the self-host brain** — it models the
"always-on cloud DR endpoint or lightweight independent coordinator" from the consensus
plan (line 59). Killing the self-host node does *not* touch this object, so leadership can
transfer to the cloud brain while the NAS/self-host is down.

Two safety primitives live here:

* a **monotonic epoch** minted on every leadership term — the epoch *is* the fence token;
* a **lease TTL** so a silent (crashed/partitioned) leader loses authority without needing
  to cooperate. A leader must actively ``renew`` before its lease expires or it is deposed.

Determinism: all time is a :class:`LogicalClock` tick (an integer the harness advances by
hand). There is no wall clock and no randomness anywhere in this spike, so the chaos test
is fully reproducible.
"""

from __future__ import annotations

from dataclasses import dataclass


class LogicalClock:
    """An injectable monotonic clock measured in integer ticks.

    The harness advances it explicitly; nothing here ever reads the wall clock, which is
    what makes the chaos test deterministic (no flakiness from real time or ``random``).
    """

    def __init__(self, start: int = 0) -> None:
        self._now = start

    def now(self) -> int:
        return self._now

    def advance(self, ticks: int = 1) -> int:
        if ticks < 0:
            raise ValueError("clock only moves forward")
        self._now += ticks
        return self._now


@dataclass(frozen=True)
class LeaseGrant:
    """Proof of a leadership term. ``epoch`` doubles as the fence token."""

    node_id: str
    epoch: int
    expires_at: int


class LeaseError(RuntimeError):
    """Raised when a node tries to act on a lease it no longer holds."""


class LeaseStore:
    """The independent epoch authority (survives self-host/NAS death).

    Invariant it enforces: **at most one live lease exists at any tick**, and every new
    term carries a strictly greater epoch. A stale leader that comes back and tries to
    ``renew`` with its old epoch is rejected here (it lost the term) — and even if it never
    calls back in, its old epoch is already fenced at the write boundary
    (:class:`~cadence.spikes.s0_3.fence.FencedDispatcher`).
    """

    def __init__(self, ttl: int) -> None:
        if ttl <= 0:
            raise ValueError("lease ttl must be positive")
        self._ttl = ttl
        self._holder: str | None = None
        self._epoch = 0
        self._expires_at = 0

    # -- observation -----------------------------------------------------------------
    @property
    def epoch(self) -> int:
        return self._epoch

    def live_holder(self, now: int) -> str | None:
        """The node holding a currently-valid lease, or ``None`` if the lease has lapsed."""
        if self._holder is not None and now < self._expires_at:
            return self._holder
        return None

    # -- mutation --------------------------------------------------------------------
    def try_acquire(self, node_id: str, now: int) -> LeaseGrant | None:
        """Acquire or renew leadership.

        * lease free/expired -> grant a **new term** (epoch bumps) to ``node_id``;
        * ``node_id`` already holds it -> renew the same term (epoch unchanged);
        * someone else holds a live lease -> ``None`` (caller is not the leader).
        """
        holder = self.live_holder(now)
        if holder is None:
            self._holder = node_id
            self._epoch += 1
            self._expires_at = now + self._ttl
            return LeaseGrant(node_id, self._epoch, self._expires_at)
        if holder == node_id:
            self._expires_at = now + self._ttl
            return LeaseGrant(node_id, self._epoch, self._expires_at)
        return None

    def renew(self, node_id: str, epoch: int, now: int) -> LeaseGrant:
        """Extend an *owned* term. Rejects a stale leader (wrong epoch / lease lost).

        This is where a self-host that recovers believing it is still leader learns the
        truth: its epoch no longer matches, so it is deposed instead of renewed.
        """
        if self.live_holder(now) == node_id and epoch == self._epoch:
            self._expires_at = now + self._ttl
            return LeaseGrant(node_id, self._epoch, self._expires_at)
        raise LeaseError(
            f"node {node_id!r} (epoch {epoch}) is not the current leader "
            f"(holder={self._holder!r}, epoch={self._epoch})"
        )
