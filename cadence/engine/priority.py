"""Priority engine + misallocation detector (engine Component 2).

Scores open :class:`~cadence.stores.models.Task` items (joined with their
:class:`~cadence.stores.models.Deadline` rows) into a ranked :class:`PriorityView`:

* **urgency** from time-to-``due_at`` — a smooth decreasing curve with a configurable
  half-life (closer = higher; overdue = max),
* **importance** from ``Task.priority`` plus a small source-signal boost (an *explicit*
  source deadline is trusted more than an *inferred* one).

The **misallocation detector** compares the live
:class:`~cadence.engine.attention.AttentionSnapshot`
against the ranked view: if the user is IMMERSED on a low-priority item while a
materially higher-priority item is imminent, it emits a :class:`Misallocation`. This is
the canonical *"immersed in the 7-day task while the 1-day report is due"* signal.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

from cadence.engine.attention import AttentionSnapshot, AttentionState
from cadence.stores.d1 import D1Store
from cadence.stores.models import Deadline, Task

_CLOSED_STATUSES = frozenset({"done", "closed", "resolved", "cancelled", "completed"})
_STOPWORDS = frozenset(
    {"the", "and", "for", "with", "from", "into", "this", "that", "your", "our"}
)
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _tokens(text: str | None) -> set[str]:
    if not text:
        return set()
    return {t for t in _TOKEN_RE.findall(text.lower()) if len(t) >= 3 and t not in _STOPWORDS}


@dataclass(frozen=True)
class PriorityConfig:
    """Weights + curve parameters for scoring."""

    #: Hours-to-due at which urgency = 0.5 (the urgency "half-life").
    urgency_half_life_hours: float = 48.0
    #: Weight of urgency vs importance in the blended score (importance weight = 1 - this).
    urgency_weight: float = 0.6
    #: ``Task.priority`` value that maps to importance 1.0.
    priority_scale: float = 5.0
    #: Importance assumed for a task with no ``priority`` set.
    default_priority: float = 2.0
    #: Importance boost for an explicit (source-provided) deadline.
    explicit_boost: float = 0.1
    #: A neglected item is "imminent" if due within this many hours (overdue counts).
    imminent_hours: float = 48.0
    #: An item overdue by MORE than this many hours has aged out — no longer "imminent"
    #: (stops perpetual daily nudges on long-abandoned tasks).
    overdue_stale_hours: float = 168.0
    #: Minimum score gap (neglected - focused) to call it a real misallocation.
    material_gap: float = 0.15
    #: Base term for the misallocation confidence (blended with the score gap).
    misalloc_confidence_base: float = 0.5

    @property
    def importance_weight(self) -> float:
        return 1.0 - self.urgency_weight


@dataclass(frozen=True)
class PriorityItem:
    """One ranked work item with its score decomposition and provenance."""

    item_id: str
    kind: str  # "task" | "deadline"
    title: str | None
    due_at: datetime | None
    urgency: float
    importance: float
    score: float
    reasons: list[str] = field(default_factory=list)
    #: Labels that identify when the user is *working on* this item (for focus matching).
    focus_hints: set[str] = field(default_factory=set)
    #: Provenance passthrough (source event ids of the underlying task/deadline).
    source_event_ids: list[str] = field(default_factory=list)

    def is_imminent(
        self, now: datetime, horizon_hours: float, stale_hours: float | None = None
    ) -> bool:
        if self.due_at is None:
            return False
        hours_to = (self.due_at - now).total_seconds() / 3600.0
        if stale_hours is not None and hours_to < -stale_hours:
            return False  # overdue so long it has aged out
        return hours_to <= horizon_hours


@dataclass(frozen=True)
class PriorityView:
    """A ranked snapshot of open work, highest score first."""

    now: datetime
    items: list[PriorityItem]

    def top(self) -> PriorityItem | None:
        return self.items[0] if self.items else None


@dataclass(frozen=True)
class Misallocation:
    """A detected attention/priority mismatch."""

    focused_item: PriorityItem
    neglected_item: PriorityItem
    gap: float
    confidence: float
    now: datetime
    reason: str = ""


class PriorityEngine:
    """Builds a :class:`PriorityView` from open tasks/deadlines and detects misallocation."""

    def __init__(self, d1: D1Store, config: PriorityConfig | None = None) -> None:
        self.d1 = d1
        self.config = config or PriorityConfig()

    # -- scoring ------------------------------------------------------------ #

    def _urgency(self, due_at: datetime | None, now: datetime) -> float:
        if due_at is None:
            return 0.0
        hours_to = (due_at - now).total_seconds() / 3600.0
        if hours_to <= 0:
            return 1.0
        h = self.config.urgency_half_life_hours
        return _clamp(h / (h + hours_to))

    def _importance(self, priority: int | None, explicit: bool) -> float:
        p = self.config.default_priority if priority is None else float(priority)
        base = _clamp(p / self.config.priority_scale)
        if explicit:
            base += self.config.explicit_boost
        return _clamp(base)

    def rank(self, now: datetime) -> PriorityView:
        """Rank all open tasks (joined with their deadlines) by blended score."""
        cfg = self.config
        items: list[PriorityItem] = []
        with self.d1.session() as session:
            tasks = session.query(Task).all()
            deadlines = session.query(Deadline).all()
            by_task: dict[str, list[Deadline]] = {}
            for d in deadlines:
                if d.task_id is not None:
                    by_task.setdefault(d.task_id, []).append(d)

            for task in tasks:
                if (task.status or "open").lower() in _CLOSED_STATUSES:
                    continue
                tdl = by_task.get(task.id, [])
                chosen = self._earliest(tdl)
                due_at = chosen.due_at if chosen else None
                explicit = bool(chosen and chosen.origin == "explicit")
                urgency = self._urgency(due_at, now)
                importance = self._importance(task.priority, explicit)
                score = round(
                    cfg.urgency_weight * urgency + cfg.importance_weight * importance, 4
                )
                hints = _tokens(task.title) | _tokens(task.summary)
                items.append(
                    PriorityItem(
                        item_id=task.id, kind="task", title=task.title, due_at=due_at,
                        urgency=round(urgency, 4), importance=round(importance, 4), score=score,
                        reasons=self._reasons(urgency, importance, due_at, now),
                        focus_hints=hints,
                        source_event_ids=list(task.source_event_ids or []),
                    )
                )

            # Standalone deadlines (not attached to a task) are items too.
            for d in deadlines:
                if d.task_id is not None:
                    continue
                urgency = self._urgency(d.due_at, now)
                importance = self._importance(None, d.origin == "explicit")
                score = round(
                    cfg.urgency_weight * urgency + cfg.importance_weight * importance, 4
                )
                items.append(
                    PriorityItem(
                        item_id=d.id, kind="deadline", title=d.summary, due_at=d.due_at,
                        urgency=round(urgency, 4), importance=round(importance, 4), score=score,
                        reasons=self._reasons(urgency, importance, d.due_at, now),
                        focus_hints=_tokens(d.summary),
                        source_event_ids=list(d.source_event_ids or []),
                    )
                )

        items.sort(key=lambda it: it.score, reverse=True)
        return PriorityView(now=now, items=items)

    @staticmethod
    def _earliest(deadlines: list[Deadline]) -> Deadline | None:
        """Pick the governing deadline: prefer an explicit non-diverging one (M1
        convention: explicit-over-inferred), else the earliest of what's left."""
        dated = [d for d in deadlines if d.due_at is not None]
        if not dated:
            return None
        explicit = [d for d in dated if d.origin == "explicit" and not d.divergence_flag]
        pool = explicit or dated
        return min(pool, key=lambda d: d.due_at)

    @staticmethod
    def _reasons(
        urgency: float, importance: float, due_at: datetime | None, now: datetime
    ) -> list[str]:
        reasons: list[str] = []
        if due_at is None:
            reasons.append("no deadline")
        else:
            hours = (due_at - now).total_seconds() / 3600.0
            reasons.append("overdue" if hours <= 0 else f"due in {hours:.0f}h")
        reasons.append(f"urgency={urgency:.2f}")
        reasons.append(f"importance={importance:.2f}")
        return reasons

    # -- misallocation ------------------------------------------------------ #

    def detect_misallocation(
        self,
        snapshot: AttentionSnapshot,
        view: PriorityView,
        *,
        min_focus_seconds: float | None = None,
    ) -> list[Misallocation]:
        """Detect an immersed-on-low-priority-while-higher-is-imminent mismatch.

        ``min_focus_seconds`` gates on *genuine* immersion: if given, a focus shorter than
        it is treated as not-yet-immersed and produces no finding (so a brief glance at a
        single app can't trigger a nudge even though its state is IMMERSED).
        """
        cfg = self.config
        if snapshot.state is not AttentionState.IMMERSED or not snapshot.focus_target:
            return []
        if min_focus_seconds is not None:
            dwell = snapshot.focus_duration.total_seconds() if snapshot.focus_duration else 0.0
            if dwell < min_focus_seconds:
                return []

        matches = [it for it in view.items if _matches(snapshot.focus_target, it)]
        if not matches:
            return []  # focused on something outside our tracked work — cannot judge
        focused = max(matches, key=lambda it: it.score)

        imminent = [
            it for it in view.items
            if it.item_id != focused.item_id
            and it.is_imminent(view.now, cfg.imminent_hours, cfg.overdue_stale_hours)
        ]
        if not imminent:
            return []
        neglected = max(imminent, key=lambda it: it.score)

        # Priority-inversion guard: only nudge to switch when the neglected item is at
        # least as *important* as what the user is on. A blended-score gap driven purely by
        # urgency (a low-priority item due in 2h) must NOT pull the user off higher-priority
        # work — that would be the engine actively harming prioritization.
        if neglected.importance < focused.importance:
            return []

        gap = round(neglected.score - focused.score, 4)
        if gap < cfg.material_gap:
            return []

        confidence = round(
            _clamp(min(snapshot.confidence, cfg.misalloc_confidence_base + gap), 0.0, 0.99), 3
        )
        reason = (
            f"immersed on '{focused.title}' (score {focused.score:.2f}) while "
            f"'{neglected.title}' (score {neglected.score:.2f}) is imminent"
        )
        return [
            Misallocation(
                focused_item=focused, neglected_item=neglected, gap=gap,
                confidence=confidence, now=view.now, reason=reason,
            )
        ]


def _matches(focus_target: str, item: PriorityItem) -> bool:
    """True if the attention ``focus_target`` plausibly refers to ``item``."""
    ft = focus_target.strip().lower()
    if item.title and ft == item.title.strip().lower():
        return True
    return bool(_tokens(focus_target) & item.focus_hints)


__all__ = [
    "PriorityConfig",
    "PriorityItem",
    "PriorityView",
    "PriorityEngine",
    "Misallocation",
]
