"""Nudge Governor (engine Component 3).

Decides whether a finding becomes a nudge. Design goals (per the M3 plan):

* **Precision-first with a recall floor.** A per-category confidence threshold gates
  firing (precision). The threshold can never rise above :data:`GovernorConfig.recall_ceiling`,
  and any finding at/above :data:`GovernorConfig.recall_floor_confidence` fires
  *unconditionally* — so a very strong signal is never silently dropped and "silence" is
  measurable (:meth:`recall_estimate`).
* **A3 shadow vs live.** In ``shadow`` mode proposed nudges are collected in
  :attr:`proposals` and **no** ``nudge`` row is written; in ``live`` mode a deliverable
  :class:`~cadence.stores.models.Nudge` row is persisted (``delivered_at`` set).
* **Idempotency.** One nudge per condition — dedupe via ``Nudge.idempotency_key`` (a hash
  of the condition), so re-evaluating the same tick doesn't double-fire.
* **Feedback loop.** :meth:`record_feedback` writes a :class:`~cadence.stores.models.Feedback`
  row and nudges the per-category threshold (Thanks → lower the bar, Dismiss → raise it).
  :meth:`thanks_rate` and :meth:`recall_estimate` expose the precision/recall proxies.
* **Device-care rule path.** :meth:`consider_device_care` is a *separate*, deterministic
  rule set over telemetry facts (low battery → a care nudge), not the inference path.

The LLM/receptiveness refinement is a documented **seam** (``receptiveness_hook``,
default off), mirroring ``RuleDeadlineExtractor.llm_hook``.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from cadence.brain.facts import FactGraph, FactInput
from cadence.engine.attention import AttentionSnapshot
from cadence.engine.priority import Misallocation
from cadence.stores.d1 import D1Store
from cadence.stores.models import Fact, Feedback, Nudge

_MISALLOCATION = "attention.misallocation"
_DEVICE_CARE = "device.care"
_INT_RE = re.compile(r"-?\d+")


class NudgeOutcome(StrEnum):
    """What the governor did with one candidate."""

    FIRED = "fired"
    SUPPRESSED = "suppressed"
    DUPLICATE = "duplicate"


@dataclass(frozen=True)
class GovernorConfig:
    """Threshold / feedback / device-care tuning."""

    base_threshold: float = 0.6
    min_threshold: float = 0.3
    #: The threshold is clamped to this ceiling so recall can't be starved by dismissals.
    recall_ceiling: float = 0.8
    #: A finding this confident fires regardless of threshold (the recall floor).
    recall_floor_confidence: float = 0.85
    thanks_delta: float = -0.05  # Thanks → keep/lower the bar
    dismiss_delta: float = 0.1   # Dismiss → raise the bar
    #: Battery percent at/below which the device-care rule fires.
    low_battery_threshold: int = 15
    #: Look-back for the newest telemetry / feedback window (seconds).
    telemetry_window_seconds: int = 3600
    #: Misallocation gap at/above which the nudge is raised to the higher priority band.
    high_priority_gap: float = 0.4
    misalloc_priority_high: int = 2
    misalloc_priority_low: int = 1


@dataclass
class ProposedNudge:
    """A nudge the governor decided to emit (shadow) or persisted (live)."""

    idempotency_key: str
    kind: str
    priority: int
    message_summary: str
    confidence: float
    fact_id: str | None = None
    source_event_ids: list[str] = field(default_factory=list)
    #: Set in live mode once persisted.
    nudge_id: str | None = None


@dataclass
class _Candidate:
    kind: str
    confidence: float
    priority: int
    message_summary: str
    idempotency_basis: tuple[object, ...]
    fact_id: str | None = None
    source_event_ids: list[str] = field(default_factory=list)


class NudgeGovernor:
    """Precision-first nudge gate with shadow/live modes, idempotency, and feedback."""

    def __init__(
        self,
        d1: D1Store,
        *,
        mode: str = "shadow",
        config: GovernorConfig | None = None,
        facts: FactGraph | None = None,
        receptiveness_hook: Callable[[_Candidate, AttentionSnapshot | None], float] | None = None,
    ) -> None:
        if mode not in ("live", "shadow"):
            raise ValueError(f"mode must be 'live' or 'shadow', got {mode!r}")
        self.d1 = d1
        self.mode = mode
        self.config = config or GovernorConfig()
        self._facts = facts
        #: Documented seam: refine a candidate's confidence from receptiveness / a future
        #: LLM signal. ``None`` (default) leaves the rule confidence untouched.
        self.receptiveness_hook = receptiveness_hook
        self.thresholds: dict[str, float] = {}
        #: Proposed nudges (populated in shadow mode; also mirrors live emissions).
        self.proposals: list[ProposedNudge] = []
        self._proposed_keys: set[str] = set()
        self._suppressed_keys: set[str] = set()
        # NOTE: fired/suppressed/duplicates are in-memory, per-process tallies (reset when
        # a new governor is constructed) — they drive :meth:`recall_estimate`, not durable
        # analytics.
        self.fired = 0
        self.suppressed = 0
        self.duplicates = 0

    # -- thresholds --------------------------------------------------------- #

    def _threshold(self, kind: str) -> float:
        raw = self.thresholds.get(kind, self.config.base_threshold)
        return max(self.config.min_threshold, min(self.config.recall_ceiling, raw))

    def _adjust(self, kind: str, delta: float) -> None:
        current = self.thresholds.get(kind, self.config.base_threshold)
        self.thresholds[kind] = max(
            self.config.min_threshold, min(self.config.recall_ceiling, current + delta)
        )

    # -- inference path: misallocation ------------------------------------- #

    def consider_misallocation(
        self, misalloc: Misallocation, snapshot: AttentionSnapshot | None, now: datetime
    ) -> ProposedNudge | None:
        """Gate a misallocation finding into a nudge (or suppress it)."""
        focused = misalloc.focused_item
        neglected = misalloc.neglected_item
        day = misalloc.now.date().isoformat()
        priority = (
            self.config.misalloc_priority_high
            if misalloc.gap >= self.config.high_priority_gap
            else self.config.misalloc_priority_low
        )
        message = (
            f"You're deep in '{focused.title}', but '{neglected.title}' is due sooner "
            f"and ranks higher — worth a look?"
        )
        candidate = _Candidate(
            kind=_MISALLOCATION,
            confidence=misalloc.confidence,
            priority=priority,
            message_summary=message,
            idempotency_basis=(_MISALLOCATION, focused.item_id, neglected.item_id, day),
            source_event_ids=list(
                dict.fromkeys(focused.source_event_ids + neglected.source_event_ids)
            ),
        )
        return self._emit(candidate, snapshot, now)

    # -- rule path: device care -------------------------------------------- #

    def consider_device_care(self, now: datetime) -> list[ProposedNudge]:
        """Deterministic rule path: low-battery telemetry facts → device-care nudges.

        Clearly separate from the inference path — rule findings carry confidence 1.0 and
        so always fire (subject only to idempotency).
        """
        emitted: list[ProposedNudge] = []
        for device_id, level, charging, fact_id in self._latest_battery_by_device(now):
            # Suppress on the *latest* reading: a newer "charging" observation must beat a
            # stale discharge reading (don't nag someone who already plugged in).
            if charging or level > self.config.low_battery_threshold:
                continue
            day = now.date().isoformat()
            candidate = _Candidate(
                kind=_DEVICE_CARE,
                confidence=1.0,
                priority=1,
                message_summary=f"Battery low ({level}%) on {device_id} — consider charging.",
                idempotency_basis=(_DEVICE_CARE, device_id, "low_battery", day),
                fact_id=fact_id,
            )
            result = self._emit(candidate, None, now)
            if result is not None:
                emitted.append(result)
        return emitted

    def _latest_battery_by_device(self, now: datetime) -> list[tuple[str, int, bool, str]]:
        """Newest ``device.power`` reading per device as ``(device, level, charging, fact_id)``.

        The newest reading always wins regardless of charging state, so a fresh "charging"
        observation supersedes an older discharge reading (the suppression decision is made
        by the caller on this latest state).
        """
        window_start = now - timedelta(seconds=self.config.telemetry_window_seconds)
        latest: dict[str, tuple[datetime, int, bool, str]] = {}
        with self.d1.session() as session:
            rows = session.execute(
                select(Fact)
                .where(Fact.kind == "device.power")
                .where(Fact.created_at >= window_start)
                .where(Fact.created_at <= now)
                .order_by(Fact.created_at)
            ).scalars().all()
            for f in rows:
                level = _parse_int(f.object_label)
                if level is None:
                    continue
                charging = bool(f.summary and "charging" in f.summary.lower())
                device = f.subject_id or "device"
                prev = latest.get(device)
                if prev is None or f.created_at >= prev[0]:
                    latest[device] = (f.created_at, level, charging, f.id)
        return [(dev, lvl, chg, fid) for dev, (_, lvl, chg, fid) in latest.items()]

    # -- emission ----------------------------------------------------------- #

    def _emit(
        self, candidate: _Candidate, snapshot: AttentionSnapshot | None, now: datetime
    ) -> ProposedNudge | None:
        if self.receptiveness_hook is not None:
            candidate.confidence = _clamp(self.receptiveness_hook(candidate, snapshot))

        key = _hash_key(candidate.idempotency_basis)

        # Idempotency: never fire the same condition twice.
        if key in self._proposed_keys or self._existing_nudge(key):
            self.duplicates += 1
            return None

        threshold = self._threshold(candidate.kind)
        fires = (
            candidate.confidence >= self.config.recall_floor_confidence
            or candidate.confidence >= threshold
        )
        if not fires:
            # Dedupe suppressions too: the same condition suppressed every tick must not
            # keep dragging the recall estimate down — count each distinct condition once.
            if key not in self._suppressed_keys:
                self._suppressed_keys.add(key)
                self.suppressed += 1
            return None

        proposed = ProposedNudge(
            idempotency_key=key,
            kind=candidate.kind,
            priority=candidate.priority,
            message_summary=candidate.message_summary,
            confidence=candidate.confidence,
            fact_id=candidate.fact_id,
            source_event_ids=candidate.source_event_ids,
        )
        if self.mode == "live":
            try:
                proposed.nudge_id = self._persist(proposed, now)
            except IntegrityError:
                # TOCTOU: a concurrent tick inserted the same idempotency_key between our
                # existence check and this insert. The unique constraint held — treat it as
                # a duplicate rather than crashing the tick.
                self.duplicates += 1
                return None
        self._proposed_keys.add(key)
        self.proposals.append(proposed)
        self.fired += 1
        return proposed

    def _persist(self, proposed: ProposedNudge, now: datetime) -> str:
        # The Nudge table has no provenance columns, so provenance (source_event_ids,
        # confidence) is upheld through a linked structured fact referenced by fact_id.
        if proposed.fact_id is None:
            proposed.fact_id = self._provenance_fact(proposed)
        nudge = Nudge(
            fact_id=proposed.fact_id,
            kind=proposed.kind,
            priority=proposed.priority,
            message_summary=proposed.message_summary,
            idempotency_key=proposed.idempotency_key,
            scheduled_at=now,
            delivered_at=now,  # governor committed it for delivery (transport is a downstream seam)
        )
        self.d1.write(nudge)
        return nudge.id

    def _provenance_fact(self, proposed: ProposedNudge) -> str:
        """Mint a structured provenance fact carrying the nudge's ``source_event_ids``."""
        facts = self._facts or FactGraph(self.d1)
        fact = facts.assert_fact(
            FactInput(
                kind="nudge",
                subject_type="nudge",
                predicate=proposed.kind,
                object_label=proposed.kind,
                confidence_value=proposed.confidence,
                confidence_type="inferred",
                source_event_ids=list(proposed.source_event_ids),
                summary=proposed.message_summary,
                dedupe_key=hashlib.sha256(
                    f"nudge|{proposed.idempotency_key}".encode()
                ).hexdigest(),
            )
        )
        return fact.id

    def _existing_nudge(self, key: str) -> bool:
        with self.d1.session() as session:
            return (
                session.execute(
                    select(Nudge.id).where(Nudge.idempotency_key == key)
                ).first()
                is not None
            )

    # -- feedback + metrics ------------------------------------------------- #

    def record_feedback(
        self, nudge_id: str, kind: str, note: str | None = None
    ) -> Feedback:
        """Record Thanks/Dismiss on a nudge and adjust its category threshold."""
        if kind not in ("thanks", "dismiss"):
            raise ValueError(f"feedback kind must be 'thanks' or 'dismiss', got {kind!r}")
        with self.d1.session() as session:
            nudge = session.get(Nudge, nudge_id)
            if nudge is None:
                # Never blindly adjust a category for an unknown nudge — that would move the
                # wrong threshold on a bogus/foreign id.
                raise ValueError(f"unknown nudge_id {nudge_id!r}")
            category = nudge.kind
        weight = 1.0 if kind == "thanks" else -1.0
        fb = Feedback(nudge_id=nudge_id, signal=kind, weight=weight, note_summary=note)
        self.d1.write(fb)
        delta = self.config.thanks_delta if kind == "thanks" else self.config.dismiss_delta
        self._adjust(category, delta)
        return fb

    def thanks_rate(self) -> float | None:
        """Thanks / (Thanks + Dismiss) over all recorded feedback; ``None`` if no feedback."""
        with self.d1.session() as session:
            counts = dict(
                session.execute(
                    select(Feedback.signal, func.count())
                    .where(Feedback.signal.in_(("thanks", "dismiss")))
                    .group_by(Feedback.signal)
                ).all()
            )
        thanks = counts.get("thanks", 0)
        dismiss = counts.get("dismiss", 0)
        total = thanks + dismiss
        if total == 0:
            return None
        return round(thanks / total, 4)

    def recall_estimate(self) -> float:
        """Fired / (Fired + Suppressed) — the fraction of candidate findings acted on.

        The recall *floor* guarantees any finding at/above
        :data:`GovernorConfig.recall_floor_confidence` is counted in the numerator (it can
        never be suppressed), so this proxy can never be gamed to zero by raising the bar.
        Both tallies dedupe by condition-key, so re-suppressing the same finding every tick
        does not distort the ratio. Returns 1.0 when nothing has been evaluated (no misses
        possible yet). Tallies are in-memory and per-process (see :meth:`__init__`).
        """
        denom = self.fired + self.suppressed
        if denom == 0:
            return 1.0
        return round(self.fired / denom, 4)


def _parse_int(text: str | None) -> int | None:
    if text is None:
        return None
    m = _INT_RE.search(text)
    return int(m.group()) if m else None


def _hash_key(basis: tuple[object, ...]) -> str:
    return hashlib.sha256("|".join(str(x) for x in basis).encode("utf-8")).hexdigest()


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


__all__ = [
    "NudgeGovernor",
    "GovernorConfig",
    "ProposedNudge",
    "NudgeOutcome",
]
