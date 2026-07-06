"""Capture harness — accumulates Events into a time-ordered signal log.

Deliberately thin: it reuses the existing adapter framework
(:class:`cadence.adapters.base.Adapter`) rather than reinventing capture. A source is
anything exposing ``.emit() -> Iterator[Event]`` (in practice a reference adapter —
GitHub/Google Calendar/Email — reading a fixture, no live network call) or a bare
iterable of already-built :class:`~cadence.adapters.base.Event` objects for synthetic
edge cases. The harness's only job is to accumulate + time-order what it's given into a
:class:`SignalLog` — the shape the S0.2 calibration harness consumes.

This is capture-time plumbing only, not a task list: nothing here schedules nudges,
tracks task/obligation state, or persists anything beyond the in-memory log for this
spike's own use. It is not wired into the live ingestion pipeline
(:mod:`cadence.ingest.pipeline`).
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime

from cadence.adapters.base import Adapter, Event


@dataclass
class SignalLog:
    """A time-ordered accumulation of Events from one or more sources."""

    events: list[Event] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.events)

    def __iter__(self) -> Iterator[Event]:
        return iter(self.events)

    def by_source(self, source: str) -> list[Event]:
        """All events from a given ``Event.source`` (e.g. ``"github"``), in log order."""
        return [e for e in self.events if e.source == source]

    def by_dedupe_id(self) -> dict[str, Event]:
        """Index events by their cross-device ``dedupe_id`` — the join key gold labels
        (:mod:`cadence.spikes.s0_0.labeling`) attach to."""
        return {e.dedupe_id: e for e in self.events if e.dedupe_id}


def _aware(ts: datetime) -> datetime:
    """Coerce a naive timestamp to UTC for ordering purposes only.

    Some sources report a bare date with no offset (e.g. an all-day calendar event's
    ``start``), which round-trips through ``Event.occurred_at`` as a naive datetime.
    Naive and aware datetimes aren't comparable, so the sort key must normalize -- the
    Event object itself is left untouched.
    """
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)


def _sort_key(event: Event) -> tuple:
    ts = event.occurred_at or event.ingested_at
    return (_aware(ts), event.source, event.event_id)


class CaptureHarness:
    """Accumulates Events from one or more sources into a single time-ordered
    :class:`SignalLog`.

    Each source is either an :class:`~cadence.adapters.base.Adapter` instance (its
    ``emit()`` is called) or a plain ``Iterable[Event]`` (consumed as-is) — the latter
    is how synthetic bootstrap-labeling edge cases (see
    :mod:`cadence.spikes.s0_0.sample`) get mixed in alongside real adapter fixtures.
    """

    def __init__(self) -> None:
        self._sources: list[Adapter | Iterable[Event]] = []

    def add_source(self, source: Adapter | Iterable[Event]) -> None:
        self._sources.append(source)

    def capture(self) -> SignalLog:
        """Drain every registered source and return one time-ordered :class:`SignalLog`."""
        events: list[Event] = []
        for source in self._sources:
            emit = getattr(source, "emit", None)
            events.extend(emit() if callable(emit) else source)
        events.sort(key=_sort_key)
        return SignalLog(events=events)


__all__ = ["SignalLog", "CaptureHarness"]
