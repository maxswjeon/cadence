"""Typed-row projection.

The ingestion pipeline always records a generic provenance :class:`~cadence.stores.
models.Fact` for every event. This module adds, on top of that, a **projection** step
that maps *known* event kinds into the typed relational tables (``calendar_event``,
``task``) so priority/deadline/schedule queries have first-class rows — closing
milestone task 7 ("adapters parse into calendar_event/task/deadline rows").

Design
------
* **Pluggable, registered by kind.** A :class:`ProjectionRegistry` maps an event
  ``kind`` string to a :data:`Projector` callable. New adapters add mappings via
  ``registry.register(kind, projector)`` — no edit to the pipeline core.
* **Additive + conservative.** Projection never replaces the Fact; it only runs for
  kinds with a clean mapping, and returns ``[]`` (Fact-only) for anything ambiguous
  (e.g. a non-actionable email).
* **Boundary + provenance.** Projectors emit ORM instances only from ``Event``'s
  structured fields + non-verbatim summary; the pipeline writes them through
  :meth:`~cadence.stores.d1.D1Store.write_all`, so the raw boundary is enforced and
  every projected row carries provenance (source-event id, NAS pointer, confidence).
* **Deadlines** continue to flow through the :class:`~cadence.brain.deadlines.
  DeadlineExtractor`; projectors here do **not** emit ``deadline`` rows (no dup).

Expected ``Event.structured`` keys (convention for the reference adapters)
-------------------------------------------------------------------------
``calendar.event`` → ``title``, ``starts_at``, ``ends_at`` (datetime or ISO str),
``all_day`` (bool), ``status``, ``location`` (label), ``source_event_id``.
``github.issue`` / ``github.pull_request`` / ``github.review_request`` /
``email.message`` → ``title``, ``status``, ``priority``, ``source_event_id`` and, for
email, ``actionable`` (bool — only actionable emails become tasks).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, TypeVar

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from cadence.adapters.base import Event
from cadence.stores.models import CalendarEvent, Place, SourceAccount, Task

if TYPE_CHECKING:  # pragma: no cover
    from cadence.stores.d1 import D1Store

#: A projector turns one event into zero or more typed ORM rows (unpersisted).
Projector = Callable[[Event, "ProjectionContext"], list[object]]

_TITLE_MAX = 255

_M = TypeVar("_M")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _coerce_dt(value: object) -> datetime | None:
    """Accept a datetime or an ISO-8601 string; return a datetime or ``None``."""
    if value is None or isinstance(value, datetime):
        return value
    if isinstance(value, str):
        text = value.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(text)
        except ValueError:
            return None
    return None


def _title(event: Event) -> str | None:
    """Pick a title from structured ``title`` or the non-verbatim summary (bounded)."""
    raw = event.structured.get("title") or event.summary
    if raw is None:
        return None
    return str(raw)[:_TITLE_MAX]


def _provenance(event: Event) -> dict[str, object]:
    """Provenance columns shared by every projected typed row."""
    return {
        "confidence_value": event.confidence,
        "confidence_type": "observed" if event.confidence is not None else None,
        "source_event_ids": [event.event_id],
        "raw_evidence_id": event.raw_evidence_ref,
        "raw_evidence_hash": event.payload_hash,
        "summary": event.summary,
    }


# --------------------------------------------------------------------------- #
# Projection context (shared, idempotent entity resolution)
# --------------------------------------------------------------------------- #


@dataclass
class ProjectionContext:
    """Resolves shared entities (source accounts, places) for projectors.

    Resolution is idempotent get-or-create keyed on natural keys, so re-ingesting the
    same account/place does not create duplicates. Created rows go through
    :meth:`D1Store.write` (raw-boundary enforced, replicated).

    The get-or-create is a select-then-insert, which race under concurrent callers
    that both miss the initial select. It is made safe by relying on the DB-level
    unique constraints on ``(source_account.provider, source_account.account_ref)``
    and ``place.label``: on an :class:`~sqlalchemy.exc.IntegrityError` from the insert,
    :meth:`_get_or_create` re-selects and returns the row the other writer created
    instead of raising, so re-ingestion under interleaving still yields exactly one row.
    """

    d1: D1Store

    def _get_or_create(
        self,
        model: type[_M],
        *,
        select_stmt,
        build: Callable[[], _M],
    ) -> _M:
        with self.d1.session() as session:
            existing = session.execute(select_stmt).scalar_one_or_none()
            if existing is not None:
                return existing
        instance = build()
        try:
            self.d1.write(instance)
        except IntegrityError:
            with self.d1.session() as session:
                existing = session.execute(select_stmt).scalar_one_or_none()
                if existing is None:
                    raise
                return existing
        return instance

    def source_account_id(self, event: Event) -> str | None:
        """Get-or-create the ``source_account`` row for this event's provider+account."""
        if not event.account_ref:
            return None
        acct = self._get_or_create(
            SourceAccount,
            select_stmt=select(SourceAccount).where(
                SourceAccount.provider == event.source,
                SourceAccount.account_ref == event.account_ref,
            ),
            build=lambda: SourceAccount(
                provider=event.source,
                account_ref=event.account_ref,
                acquisition_tier=str(event.acquisition_tier),
            ),
        )
        return acct.id

    def place_id(self, label: str | None) -> str | None:
        """Get-or-create a ``place`` row for a location label, if present."""
        if not label:
            return None
        label = str(label)[:255]
        place = self._get_or_create(
            Place,
            select_stmt=select(Place).where(Place.label == label),
            build=lambda: Place(label=label, kind="location"),
        )
        return place.id


# --------------------------------------------------------------------------- #
# Built-in projectors
# --------------------------------------------------------------------------- #


def project_calendar_event(event: Event, ctx: ProjectionContext) -> list[object]:
    """``calendar.event`` → a ``calendar_event`` row."""
    s = event.structured
    row = CalendarEvent(
        source_account_id=ctx.source_account_id(event),
        source_event_id=s.get("source_event_id") or event.event_id,
        title=_title(event),
        starts_at=_coerce_dt(s.get("starts_at")),
        ends_at=_coerce_dt(s.get("ends_at")),
        all_day=bool(s.get("all_day", False)),
        status=s.get("status"),
        place_id=ctx.place_id(s.get("location")),
        **_provenance(event),
    )
    return [row]


def project_task(event: Event, ctx: ProjectionContext) -> list[object]:
    """GitHub issue/PR/review-request → a ``task`` row."""
    s = event.structured
    row = Task(
        source_account_id=ctx.source_account_id(event),
        source_event_id=s.get("source_event_id") or event.event_id,
        title=_title(event),
        status=s.get("status", "open"),
        priority=s.get("priority"),
        **_provenance(event),
    )
    return [row]


def project_email_task(event: Event, ctx: ProjectionContext) -> list[object]:
    """``email.message`` → a ``task`` row, but only when it implies an action.

    Conservative: an email becomes a task only if the adapter marked it
    ``structured["actionable"] == True``; otherwise it stays Fact-only.
    """
    if not event.structured.get("actionable"):
        return []
    return project_task(event, ctx)


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #


class ProjectionRegistry:
    """Maps event ``kind`` → :data:`Projector`. Pluggable; new adapters register kinds."""

    def __init__(self) -> None:
        self._by_kind: dict[str, Projector] = {}

    def register(self, kind: str, projector: Projector) -> None:
        """Register (or override) the projector for an event ``kind``."""
        self._by_kind[kind] = projector

    def unregister(self, kind: str) -> None:
        self._by_kind.pop(kind, None)

    def kinds(self) -> list[str]:
        return sorted(self._by_kind)

    def project(self, event: Event, ctx: ProjectionContext) -> list[object]:
        """Return typed rows for ``event`` (empty if no projector or nothing to map)."""
        projector = self._by_kind.get(event.kind)
        if projector is None:
            return []
        return projector(event, ctx)


def default_projection_registry() -> ProjectionRegistry:
    """A registry pre-loaded with the built-in kind → typed-row mappings."""
    reg = ProjectionRegistry()
    reg.register("calendar.event", project_calendar_event)
    reg.register("github.issue", project_task)
    reg.register("github.pull_request", project_task)
    reg.register("github.review_request", project_task)
    reg.register("email.message", project_email_task)
    return reg


__all__ = [
    "Projector",
    "ProjectionContext",
    "ProjectionRegistry",
    "default_projection_registry",
    "project_calendar_event",
    "project_task",
    "project_email_task",
]
