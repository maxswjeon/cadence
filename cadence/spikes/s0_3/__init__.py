"""S0.3 — Failover/fence spike (Decision C / AC-6).

A self-contained, deterministic model of Cadence's runtime-authority mechanism:

* :mod:`.authority` — a single-leader lease/epoch store that is **independent of the
  self-host** (survives NAS death) and mints a monotonic epoch per term (the fence token);
* :mod:`.fence` — a :class:`~cadence.spikes.s0_3.fence.FencedDispatcher` that rejects
  stale-epoch writes and dedupes idempotent nudge ids (exactly-once delivery);
* :mod:`.cluster` — the brain leader-candidates and the edge-buffering device;
* :mod:`.chaos` — the Normal -> NAS-down -> cloud-takeover -> stale-recovery chaos run.

See ``.omc/research/spikes/s0_3.md`` for the method and PASS/FAIL verdict.
"""

from __future__ import annotations

from .authority import LeaseError, LeaseGrant, LeaseStore, LogicalClock
from .chaos import ChaosReport, ChaosSimulator, Snapshot
from .cluster import BrainNode, Device
from .fence import DispatchResult, FencedDispatcher, Nudge, make_nudge_id

__all__ = [
    "BrainNode",
    "ChaosReport",
    "ChaosSimulator",
    "Device",
    "DispatchResult",
    "FencedDispatcher",
    "LeaseError",
    "LeaseGrant",
    "LeaseStore",
    "LogicalClock",
    "Nudge",
    "Snapshot",
    "make_nudge_id",
]
