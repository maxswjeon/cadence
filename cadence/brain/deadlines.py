"""Deadline-extractor hook point.

The **deadline parser is built in a later wave** — this module defines only the
contract it plugs into so the ingestion pipeline can call it without knowing the
implementation. The parser will:

* read an :class:`~cadence.adapters.base.Event`'s structured fields / non-verbatim
  summary (never verbatim raw — that stays in NAS),
* emit :class:`DeadlineCandidate` rows tagged with ``origin`` (``"explicit"`` from a
  source-provided due date vs ``"inferred"`` from text), a confidence value+type, and
* set ``divergence_flag`` when an inferred deadline disagrees with an explicit one.

Convention the parser must honor: **prefer explicit-source deadlines over inferred**,
and flag divergence rather than silently overriding.

In M1 the pipeline uses :class:`NullDeadlineExtractor` (extracts nothing) so the spine
runs end-to-end without the parser present.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from cadence.adapters.base import Event


@dataclass
class DeadlineCandidate:
    """A deadline derived from an event, ready to become a ``deadline`` row."""

    due_at: datetime
    origin: str = "explicit"  # "explicit" | "inferred"
    confidence_value: float | None = None
    confidence_type: str | None = None
    divergence_flag: bool = False
    source_event_ids: list[str] = field(default_factory=list)
    summary: str | None = None


class DeadlineExtractor(ABC):
    """Contract for the (later-wave) deadline parser.

    Implementations are pure functions over an :class:`Event` → deadline candidates;
    they must not perform I/O or reach into raw NAS evidence beyond what the Event
    exposes. The inference engine attaches here in a future milestone.
    """

    @abstractmethod
    def extract(self, event: Event) -> list[DeadlineCandidate]:
        """Return zero or more deadline candidates derived from ``event``."""


class NullDeadlineExtractor(DeadlineExtractor):
    """No-op extractor used by the M1 spine (the real parser replaces it)."""

    def extract(self, event: Event) -> list[DeadlineCandidate]:  # noqa: ARG002
        return []


# --------------------------------------------------------------------------- #
# Rule/heuristic deadline parser (M1 — no LLM call)
# --------------------------------------------------------------------------- #

#: Structured keys checked (in order) for a source-provided due date — calendar/task
#: adapters populate one of these on ``Event.structured`` when they know the due date.
#: ``due_on``/``milestone_due_on`` are the GitHub adapter's issue-due-date/milestone
#: field names.
_EXPLICIT_STRUCTURED_KEYS = ("due_at", "due_date", "deadline", "due_on", "milestone_due_on")

#: Keywords that mark a summary sentence as containing an (inferred) deadline phrase.
_DEADLINE_KEYWORD_RE = re.compile(r"\b(due|by|deadline|마감)\b", re.IGNORECASE)

_D_MINUS_RE = re.compile(r"\bD-(\d+)\b", re.IGNORECASE)
_ISO_DATE_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2}))?\b")
_KOREAN_DATE_RE = re.compile(r"(\d{1,2})\s*월\s*(\d{1,2})\s*일")
_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
    "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}  # fmt: skip
_MONTH_DAY_RE = re.compile(
    r"\b(" + "|".join(_MONTHS) + r")\.?\s+(\d{1,2})(?:,?\s*(\d{4}))?\b", re.IGNORECASE
)
_SLASH_DATE_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b")


def _coerce_datetime(value: object) -> datetime | None:
    """Best-effort coercion of a structured-field value to an aware ``datetime``."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def _explicit_from_structured(event: Event) -> DeadlineCandidate | None:
    """Pull a source-provided due date straight off ``Event.structured``, if present."""
    for key in _EXPLICIT_STRUCTURED_KEYS:
        due_at = _coerce_datetime(event.structured.get(key))
        if due_at is None:
            continue
        return DeadlineCandidate(
            due_at=due_at,
            origin="explicit",
            confidence_value=1.0,
            confidence_type="source",
            source_event_ids=[event.event_id],
            summary=f"explicit due date from '{key}' ({event.kind})",
        )
    return None


def _extract_date_from_text(text: str, reference: datetime) -> tuple[datetime | None, str | None]:
    """Find the first recognizable date in ``text``, filling a missing year from ``reference``."""
    m = _ISO_DATE_RE.search(text)
    if m:
        year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
        hour = int(m.group(4)) if m.group(4) else 23
        minute = int(m.group(5)) if m.group(5) else 59
        return _safe_datetime(year, month, day, hour, minute), m.group(0)

    m = _KOREAN_DATE_RE.search(text)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        return _safe_datetime(reference.year, month, day, 23, 59), m.group(0)

    m = _MONTH_DAY_RE.search(text)
    if m:
        month = _MONTHS[m.group(1).lower()]
        day = int(m.group(2))
        year = int(m.group(3)) if m.group(3) else reference.year
        return _safe_datetime(year, month, day, 23, 59), m.group(0)

    m = _SLASH_DATE_RE.search(text)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        year = int(m.group(3)) if m.group(3) else reference.year
        if year < 100:
            year += 2000
        return _safe_datetime(year, month, day, 23, 59), m.group(0)

    return None, None


def _safe_datetime(year: int, month: int, day: int, hour: int, minute: int) -> datetime | None:
    try:
        return datetime(year, month, day, hour, minute, tzinfo=UTC)
    except ValueError:
        return None


def _infer_from_summary(event: Event) -> DeadlineCandidate | None:
    """Look for a deadline phrase ("due", "by <date>", "마감", "D-N", ...) in the summary.

    Non-verbatim by construction — this only ever reads ``Event.summary``, never raw
    NAS evidence, per the raw boundary the adapter already enforced.
    """
    text = event.summary
    if not text:
        return None
    reference = event.occurred_at or event.ingested_at

    d_minus = _D_MINUS_RE.search(text)
    if d_minus:
        due_at = reference + timedelta(days=int(d_minus.group(1)))
        return DeadlineCandidate(
            due_at=due_at,
            origin="inferred",
            confidence_value=0.6,
            confidence_type="heuristic",
            source_event_ids=[event.event_id],
            summary=f"inferred from '{d_minus.group(0)}': {text}",
        )

    if not _DEADLINE_KEYWORD_RE.search(text):
        return None

    due_at, matched = _extract_date_from_text(text, reference)
    if due_at is None:
        return None
    return DeadlineCandidate(
        due_at=due_at,
        origin="inferred",
        confidence_value=0.6,
        confidence_type="heuristic",
        source_event_ids=[event.event_id],
        summary=f"inferred from '{matched}': {text}",
    )


def _dates_diverge(a: datetime, b: datetime) -> bool:
    """Compare by calendar date, not exact timestamp.

    Text-derived dates default to end-of-day (23:59) since the summary rarely states a
    time, so an explicit ``09:00`` due date and an inferred "due <same day>" phrase must
    not be flagged as divergent just because their times differ. A different due *day*
    is always a real divergence, though.
    """
    return a.astimezone(UTC).date() != b.astimezone(UTC).date()


def _reconcile(
    explicit: DeadlineCandidate | None, inferred: list[DeadlineCandidate]
) -> list[DeadlineCandidate]:
    """Apply the explicit-over-inferred convention.

    If both are present and agree (same calendar date, see :func:`_dates_diverge`), the
    explicit candidate already covers it and the inferred one is dropped. If they
    disagree, both are returned with ``divergence_flag=True`` — the inferred value is
    never silently used to override the explicit one.
    """
    if explicit is None:
        return inferred
    if not inferred:
        return [explicit]

    diverging = [c for c in inferred if _dates_diverge(explicit.due_at, c.due_at)]
    if not diverging:
        return [explicit]

    explicit.divergence_flag = True
    for c in diverging:
        c.divergence_flag = True
    return [explicit, *diverging]


class RuleDeadlineExtractor(DeadlineExtractor):
    """Rule/heuristic deadline parser (M1 — no LLM call).

    Two passes, reconciled per the module contract (prefer explicit, flag divergence):

    1. **Explicit** — a source-provided due date read straight off
       ``Event.structured`` (see :data:`_EXPLICIT_STRUCTURED_KEYS`), e.g. a calendar or
       task adapter that already knows the due date.
    2. **Inferred** — a deadline phrase pattern-matched out of ``Event.summary``:
       explicit dates ("2026-07-10", "7/10", "July 10", "10월 7일"), keyword-gated
       phrases ("due", "by <date>", "마감", generic "bill due" wording), and relative
       "D-N" countdowns (N days from ``occurred_at``/``ingested_at``).

    Future-inference seam
    ----------------------
    Pass ``llm_hook`` to attach a (future) LLM-backed inference strategy: a callable
    ``Event -> list[DeadlineCandidate]`` returning ``origin="inferred"`` candidates.
    When set, it runs alongside the rule-based summary inference and its candidates
    flow through the same explicit-preference/divergence reconciliation — no special
    casing needed elsewhere. Not implemented in M1; ``llm_hook=None`` (default) skips
    it entirely, so the rule-based pass is the sole inferred source.
    """

    def __init__(
        self, *, llm_hook: Callable[[Event], list[DeadlineCandidate]] | None = None
    ) -> None:
        self._llm_hook = llm_hook

    def extract(self, event: Event) -> list[DeadlineCandidate]:
        explicit = _explicit_from_structured(event)

        inferred: list[DeadlineCandidate] = []
        rule_based = _infer_from_summary(event)
        if rule_based is not None:
            inferred.append(rule_based)
        if self._llm_hook is not None:
            inferred.extend(self._llm_hook(event))

        return _reconcile(explicit, inferred)


__all__ = [
    "DeadlineCandidate",
    "DeadlineExtractor",
    "NullDeadlineExtractor",
    "RuleDeadlineExtractor",
]
