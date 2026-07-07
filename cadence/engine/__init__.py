"""Cadence attention & priority engine (Milestone 3).

The reasoning loop that turns captured D1 state into attention-state + priority +
real-time nudges:

    :class:`~cadence.engine.attention.AttentionDetector`  (Component 1)
        → :class:`~cadence.engine.priority.PriorityEngine` (Component 2)
            → :class:`~cadence.engine.governor.NudgeGovernor` (Component 3)
                driven by :class:`~cadence.engine.engine.AttentionEngine` (Component 4)

See ``cadence/engine/README.md`` for the loop, the shadow→live graduation, and the
LLM/scheduler seams.
"""

from __future__ import annotations

from cadence.engine.attention import (
    AttentionConfig,
    AttentionDetector,
    AttentionSnapshot,
    AttentionState,
)
from cadence.engine.engine import AttentionEngine, EngineTick
from cadence.engine.governor import (
    GovernorConfig,
    NudgeGovernor,
    NudgeOutcome,
    ProposedNudge,
)
from cadence.engine.priority import (
    Misallocation,
    PriorityConfig,
    PriorityEngine,
    PriorityItem,
    PriorityView,
)

__all__ = [
    "AttentionConfig",
    "AttentionDetector",
    "AttentionSnapshot",
    "AttentionState",
    "AttentionEngine",
    "EngineTick",
    "GovernorConfig",
    "NudgeGovernor",
    "NudgeOutcome",
    "ProposedNudge",
    "Misallocation",
    "PriorityConfig",
    "PriorityEngine",
    "PriorityItem",
    "PriorityView",
]
