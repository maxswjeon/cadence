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
_WEEKDAYS = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}  # fmt: skip
#: Sorted longest-name-first so the alternation prefers "thursday" over "thu" etc.
#: (harmless either way given the trailing \b, but keeps intent explicit.)
_WEEKDAY_RE = re.compile(
    r"\b(?:(this|next)\s+)?("
    + "|".join(sorted(_WEEKDAYS, key=len, reverse=True))
    + r")\b",
    re.IGNORECASE,
)

#: Separators (whitespace/colon/comma/dash) stripped before every date-match attempt —
#: free, not counted against the filler-word budget below.
_LEADING_SEP_RE = re.compile(r"^[\s:,-]*")

#: A handful of short copula/glue words that can sit between a deadline keyword and its
#: date without using up the filler-word budget (e.g. "deadline **is** 2026-07-10",
#: "due **on** Friday").
_CONNECTOR_WORD_RE = re.compile(r"^(on|is|was)\b", re.IGNORECASE)

_FILLER_WORD_RE = re.compile(r"^[A-Za-z]+")
#: How many non-connector filler words (e.g. "for", "the", "report") we'll skip past
#: looking for a date before giving up on this keyword occurrence. Bounded on purpose:
#: real phrasings like "Deadline for the report is 2026-07-10" or "due for the invoice
#: on Friday" need a few words of slack, but an unbounded scan would re-admit the
#: original bug (a keyword binding to an unrelated date anywhere later in the sentence).
#: A period/semicolon/etc. also hard-stops the scan, since it isn't in the separator or
#: filler-word character classes.
_MAX_FILLER_WORDS = 3


def _tied_date_in_clause(clause: str, reference: datetime) -> tuple[datetime | None, str | None]:
    """Find a date bound to a deadline keyword within the text right after it.

    Tries a match at the current position first; if that fails, skips one connector
    word (free) or one filler word (counted against :data:`_MAX_FILLER_WORDS`) and
    tries again. A date that never turns up within that bounded window — or that's
    separated from the keyword by clause-ending punctuation, which no separator/filler
    pattern consumes — means this keyword occurrence isn't tied to a date at all.
    """
    remaining = clause
    fillers_used = 0
    while True:
        remaining = _LEADING_SEP_RE.sub("", remaining)
        due_at, matched = _extract_date_from_text(remaining, reference)
        if due_at is not None:
            return due_at, matched

        m = _CONNECTOR_WORD_RE.match(remaining)
        if m:
            remaining = remaining[m.end() :]
            continue

        if fillers_used >= _MAX_FILLER_WORDS:
            return None, None
        m = _FILLER_WORD_RE.match(remaining)
        if not m:
            return None, None
        remaining = remaining[m.end() :]
        fillers_used += 1


def _resolve_weekday(day_name: str, modifier: str | None, reference: datetime) -> datetime:
    """Resolve a weekday name to its next calendar occurrence relative to ``reference``.

    Bare ("Friday") and "this Friday" both mean the nearest upcoming Friday — if
    ``reference`` itself falls on that weekday, that counts as already past, so the
    next occurrence is a week out. "next Monday" skips one week further still.
    """
    target = _WEEKDAYS[day_name.lower()]
    days_ahead = (target - reference.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    if modifier and modifier.lower() == "next":
        days_ahead += 7
    return reference + timedelta(days=days_ahead)


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
    """Find a recognizable date anchored at the start of ``text``.

    ``text`` is expected to already be trimmed to what immediately follows a deadline
    keyword (see :func:`_infer_from_summary`), so matches are anchored (``.match``, not
    ``.search``) — a date has to be the thing the keyword is pointing at, not just
    present somewhere later on. A missing year/day-only value is filled from
    ``reference``.
    """
    m = _ISO_DATE_RE.match(text)
    if m:
        year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
        hour = int(m.group(4)) if m.group(4) else 23
        minute = int(m.group(5)) if m.group(5) else 59
        return _safe_datetime(year, month, day, hour, minute), m.group(0)

    m = _KOREAN_DATE_RE.match(text)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        return _safe_datetime(reference.year, month, day, 23, 59), m.group(0)

    m = _MONTH_DAY_RE.match(text)
    if m:
        month = _MONTHS[m.group(1).lower()]
        day = int(m.group(2))
        year = int(m.group(3)) if m.group(3) else reference.year
        return _safe_datetime(year, month, day, 23, 59), m.group(0)

    m = _SLASH_DATE_RE.match(text)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        year = int(m.group(3)) if m.group(3) else reference.year
        if year < 100:
            year += 2000
        return _safe_datetime(year, month, day, 23, 59), m.group(0)

    m = _WEEKDAY_RE.match(text)
    if m:
        due_date = _resolve_weekday(m.group(2), m.group(1), reference)
        return _safe_datetime(due_date.year, due_date.month, due_date.day, 23, 59), m.group(0)

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

    # A date only counts if it's syntactically tied to a deadline keyword — within a
    # few words of "due/by/deadline/마감" in the same clause (see
    # :func:`_tied_date_in_clause`), not merely co-occurring somewhere else in the
    # sentence. Try every keyword occurrence in order; the first one with a bound date
    # wins.
    for kw_match in _DEADLINE_KEYWORD_RE.finditer(text):
        due_at, matched = _tied_date_in_clause(text[kw_match.end() :], reference)
        if due_at is None:
            continue
        return DeadlineCandidate(
            due_at=due_at,
            origin="inferred",
            confidence_value=0.6,
            confidence_type="heuristic",
            source_event_ids=[event.event_id],
            summary=f"inferred from '{matched}': {text}",
        )

    return None


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
       relative "D-N" countdowns (N days from ``occurred_at``/``ingested_at``), and
       keyword-gated phrases ("due", "by <date>", "마감", generic "bill due" wording)
       where the date must be bound to the keyword within the same clause — directly
       after it, after a connector word ("on"/"is"/"was"), or after up to
       :data:`_MAX_FILLER_WORDS` other words (see :func:`_tied_date_in_clause`), e.g.
       "Deadline for the report is 2026-07-10" or "due for the invoice on Friday" — not
       merely present somewhere later in the sentence. Recognized date shapes: calendar
       dates ("2026-07-10", "7/10", "July 10", "10월 7일") and relative weekday names
       ("by Friday", "due Wednesday", "next Monday" — the next occurrence of that
       weekday, or the one after if qualified with "next"). A bare date sitting
       elsewhere in the sentence near, but not tied to, a keyword does not count —
       that's a false positive, not a deadline.

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
