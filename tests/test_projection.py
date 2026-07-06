"""Tests for typed-row projection (calendar_event / task) alongside the Fact."""

from __future__ import annotations

from datetime import UTC, datetime

from cadence.adapters.base import AcquisitionTier, Event
from cadence.brain.projection import (
    ProjectionContext,
    default_projection_registry,
)
from cadence.ingest.pipeline import IngestPipeline
from cadence.obs.alarms import get_alarm_sink
from cadence.stores.models import CalendarEvent, Fact, SourceAccount, Task


def _calendar_event() -> Event:
    return Event(
        event_id="cal-1",
        source="google_calendar",
        account_ref="me@example.com",
        kind="calendar.event",
        acquisition_tier=AcquisitionTier.OAUTH,
        summary="Standup",
        confidence=0.99,
        raw_evidence_ref="a" * 64,
        payload_hash="a" * 64,
        structured={
            "title": "Team standup",
            "starts_at": "2026-07-10T09:00:00+00:00",
            "ends_at": datetime(2026, 7, 10, 9, 15, tzinfo=UTC),
            "all_day": False,
            "status": "confirmed",
            "location": "Room 4",
            "source_event_id": "gcal-abc",
        },
    )


def _github_issue() -> Event:
    return Event(
        event_id="gh-1",
        source="github",
        account_ref="octocat",
        kind="github.issue",
        acquisition_tier=AcquisitionTier.OFFICIAL_API,
        summary="issue: fix login",
        confidence=0.95,
        structured={"title": "Fix login", "status": "open", "priority": 2},
    )


def test_calendar_event_projects_typed_row(store) -> None:
    pipe = IngestPipeline(store)
    result = pipe.ingest(_calendar_event())

    assert ("calendar_event", result.fact_id) not in result.projected  # not the fact id
    tables = [t for t, _ in result.projected]
    assert "calendar_event" in tables

    with store.session() as session:
        ce = session.query(CalendarEvent).one()
        assert ce.title == "Team standup"
        assert ce.status == "confirmed"
        assert ce.starts_at is not None and ce.ends_at is not None
        assert ce.place_id is not None  # location resolved to a place row
        # provenance present
        assert ce.source_event_ids == ["cal-1"]
        assert ce.raw_evidence_id == "a" * 64
        assert ce.confidence_value == 0.99
        # source_account get-or-created + linked
        assert ce.source_account_id is not None
        # the generic Fact is ALSO written (projection is additive)
        assert session.query(Fact).count() == 1


def test_github_issue_projects_task(store) -> None:
    pipe = IngestPipeline(store)
    result = pipe.ingest(_github_issue())
    assert "task" in [t for t, _ in result.projected]
    with store.session() as session:
        task = session.query(Task).one()
        assert task.title == "Fix login"
        assert task.status == "open"
        assert task.priority == 2
        assert task.source_event_ids == ["gh-1"]
        assert task.source_account_id is not None


def test_pr_and_review_request_project_task(store) -> None:
    pipe = IngestPipeline(store)
    for kind in ("github.pull_request", "github.review_request"):
        ev = _github_issue().model_copy(update={"event_id": kind, "kind": kind})
        result = pipe.ingest(ev)
        assert "task" in [t for t, _ in result.projected]
    with store.session() as session:
        assert session.query(Task).count() == 2


def test_actionable_email_projects_task_but_plain_email_does_not(store) -> None:
    pipe = IngestPipeline(store)
    actionable = Event(
        event_id="em-1", source="gmail", account_ref="me@example.com",
        kind="email.message", summary="Please review the doc by Friday",
        structured={"title": "Review doc", "actionable": True},
    )
    plain = Event(
        event_id="em-2", source="gmail", account_ref="me@example.com",
        kind="email.message", summary="Newsletter", structured={"title": "Weekly news"},
    )
    r1 = pipe.ingest(actionable)
    r2 = pipe.ingest(plain)
    assert "task" in [t for t, _ in r1.projected]
    assert r2.projected == []  # non-actionable → Fact only
    with store.session() as session:
        assert session.query(Task).count() == 1
        assert session.query(Fact).count() == 2  # both still produce a Fact


def test_unmapped_kind_projects_nothing(store) -> None:
    pipe = IngestPipeline(store)
    ev = Event(event_id="x", source="slack", account_ref="a", kind="slack.message",
               summary="hi")
    result = pipe.ingest(ev)
    assert result.projected == []
    with store.session() as session:
        assert session.query(Task).count() == 0
        assert session.query(CalendarEvent).count() == 0
        assert session.query(Fact).count() == 1


def test_projection_is_zero_raw_to_cloud_violations(store) -> None:
    pipe = IngestPipeline(store)
    pipe.ingest(_calendar_event())
    pipe.ingest(_github_issue())
    assert get_alarm_sink().count("raw_to_cloud_violation") == 0


def test_source_account_resolution_is_idempotent(store) -> None:
    pipe = IngestPipeline(store)
    pipe.ingest(_github_issue())
    pipe.ingest(_github_issue().model_copy(update={"event_id": "gh-2"}))
    with store.session() as session:
        # same provider+account_ref reused, not duplicated
        assert session.query(SourceAccount).filter_by(account_ref="octocat").count() == 1
        assert session.query(Task).count() == 2


def test_registry_is_pluggable(store) -> None:
    """A new adapter can register a kind without touching the pipeline core."""
    reg = default_projection_registry()
    assert "calendar.event" in reg.kinds()

    calls: list[str] = []

    def _custom(event, ctx):
        calls.append(event.event_id)
        return []

    reg.register("custom.kind", _custom)
    pipe = IngestPipeline(store, projection_registry=reg)
    pipe.ingest(Event(event_id="c1", source="s", account_ref="a", kind="custom.kind"))
    assert calls == ["c1"]


def test_context_place_dedupes(store) -> None:
    ctx = ProjectionContext(store)
    p1 = ctx.place_id("HQ")
    p2 = ctx.place_id("HQ")
    assert p1 == p2 and p1 is not None
    assert ctx.place_id(None) is None
