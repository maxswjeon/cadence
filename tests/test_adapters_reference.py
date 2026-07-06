"""Tests for the reference source adapters (GitHub, Google Calendar, Email).

Each adapter is exercised against its recorded fixture (``tests/fixtures/<provider>/``)
with **no live network call**. The unit tests assert fixtures normalize into correctly
structured Events with NAS evidence pointers and that no verbatim raw content (issue/PR
body, calendar description, email body) leaks into the Event or downstream D1. The
integration test runs all three adapters' Events through the real ingestion pipeline
and asserts the right facts land in D1 with zero raw-to-cloud violations.
"""

from __future__ import annotations

import json
from pathlib import Path

from cadence.adapters.base import AdapterRegistry, Event
from cadence.adapters.email import EmailAdapter
from cadence.adapters.gcal import GoogleCalendarAdapter
from cadence.adapters.github import GitHubAdapter
from cadence.brain.facts import FactGraph
from cadence.ingest.pipeline import IngestPipeline
from cadence.obs.alarms import get_alarm_sink
from cadence.stores.models import Fact
from cadence.stores.nas import NASStore
from cadence.stores.raw_boundary import PayloadClassifier, Verdict

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _no_verbatim_leak(event: Event, *verbatim_snippets: str) -> None:
    """Assert none of the raw verbatim snippets leaked into the Event's public fields."""
    haystack = json.dumps(event.structured) + (event.summary or "")
    for snippet in verbatim_snippets:
        assert snippet not in haystack


# --------------------------------------------------------------------------- #
# GitHub
# --------------------------------------------------------------------------- #


def test_github_adapter_normalizes_fixtures(settings) -> None:
    nas = NASStore(settings)
    adapter = GitHubAdapter(
        "octocat", nas=nas, fixture_path=FIXTURES_DIR / "github" / "events.json"
    )
    raw_records = adapter.fetch()
    events = list(adapter.emit())
    assert len(events) == len(raw_records) == 3

    issue, pr, review = events
    assert issue.kind == "github.issue"
    assert issue.structured["repo"] == "octocat/hello-world"
    assert issue.structured["state"] == "open"
    assert issue.structured["due_on"] == "2026-07-15"
    assert issue.summary == "issue #42: Fix login bug"

    assert pr.kind == "github.pull_request"
    assert pr.structured["requested_reviewer_count"] == 1
    assert pr.summary == "pr #43: Add rate limiting to the public API"

    assert review.kind == "github.review_request"
    assert review.structured["reviewer"] == "hubot"

    for event, raw in zip(events, raw_records, strict=True):
        assert event.raw_evidence_ref and event.payload_hash
        assert event.raw_evidence_ref == event.payload_hash
        assert nas.exists(event.raw_evidence_ref)
        stored = json.loads(nas.get(event.raw_evidence_ref))
        assert stored == raw  # the verbatim record is retrievable byte-for-byte
        if "body" in raw:
            _no_verbatim_leak(event, raw["body"])


def test_github_adapter_registered() -> None:
    reg = AdapterRegistry()
    reg.register(GitHubAdapter)
    assert reg.get("github") is GitHubAdapter


# --------------------------------------------------------------------------- #
# Google Calendar
# --------------------------------------------------------------------------- #


def test_gcal_adapter_normalizes_fixtures(settings) -> None:
    nas = NASStore(settings)
    adapter = GoogleCalendarAdapter(
        "alice@example.com", nas=nas, fixture_path=FIXTURES_DIR / "google_calendar" / "events.json"
    )
    raw_records = adapter.fetch()
    events = list(adapter.emit())
    assert len(events) == 2

    sync, offsite = events
    assert sync.kind == "calendar.event"
    assert sync.structured["attendee_count"] == 2
    assert sync.structured["accepted_count"] == 1
    assert sync.structured["location"] == "Zoom"
    assert sync.summary == "event: Team sync"

    assert offsite.structured["all_day"] is True
    assert offsite.structured["attendee_count"] == 3

    for event, raw in zip(events, raw_records, strict=True):
        assert event.raw_evidence_ref and event.payload_hash
        assert nas.exists(event.raw_evidence_ref)
        stored = json.loads(nas.get(event.raw_evidence_ref))
        assert stored == raw
        _no_verbatim_leak(event, raw["description"])


def test_gcal_adapter_registered() -> None:
    reg = AdapterRegistry()
    reg.register(GoogleCalendarAdapter)
    assert reg.get("google_calendar") is GoogleCalendarAdapter


# --------------------------------------------------------------------------- #
# Email
# --------------------------------------------------------------------------- #


def test_email_adapter_normalizes_fixtures(settings) -> None:
    nas = NASStore(settings)
    adapter = EmailAdapter(
        "me@example.com", nas=nas, fixture_path=FIXTURES_DIR / "email" / "messages.json"
    )
    raw_records = adapter.fetch()
    events = list(adapter.emit())
    assert len(events) == 2

    budget, newsletter = events
    assert budget.kind == "email.message"
    assert budget.structured["sender"] == "manager@example.com"
    assert budget.structured["recipient_count"] == 1
    assert budget.structured["has_attachment"] is True
    assert budget.summary == "email: Please review the Q3 budget by Friday"

    assert newsletter.structured["has_attachment"] is False

    for event, raw in zip(events, raw_records, strict=True):
        assert event.raw_evidence_ref and event.payload_hash
        assert nas.exists(event.raw_evidence_ref)
        stored = json.loads(nas.get(event.raw_evidence_ref))
        assert stored == raw
        _no_verbatim_leak(event, raw["body"])


def test_email_adapter_registered() -> None:
    reg = AdapterRegistry()
    reg.register(EmailAdapter)
    assert reg.get("email") is EmailAdapter


# --------------------------------------------------------------------------- #
# Integration: all three adapters -> ingestion pipeline -> D1
# --------------------------------------------------------------------------- #


def test_reference_adapters_ingest_into_d1_with_zero_raw_violations(store, settings) -> None:
    nas = NASStore(settings)
    adapters = [
        GitHubAdapter("octocat", nas=nas, fixture_path=FIXTURES_DIR / "github" / "events.json"),
        GoogleCalendarAdapter(
            "alice@example.com",
            nas=nas,
            fixture_path=FIXTURES_DIR / "google_calendar" / "events.json",
        ),
        EmailAdapter(
            "me@example.com", nas=nas, fixture_path=FIXTURES_DIR / "email" / "messages.json"
        ),
    ]
    pipeline = IngestPipeline(store, fact_graph=FactGraph(store, nas))

    all_events = [event for adapter in adapters for event in adapter.emit()]
    results = pipeline.ingest_many(all_events)
    assert all(r.accepted for r in results)
    assert len({r.fact_id for r in results}) == len(all_events) == 7

    with store.session() as session:
        facts = session.query(Fact).all()
        assert len(facts) == 7
        kinds = {f.kind for f in facts}
        assert kinds == {
            "github.issue",
            "github.pull_request",
            "github.review_request",
            "calendar.event",
            "email.message",
        }
        for fact in facts:
            assert fact.raw_evidence_id is not None
            assert nas.exists(fact.raw_evidence_id)

    # Zero raw-to-cloud violations across the whole run.
    assert get_alarm_sink().count("raw_to_cloud_violation") == 0

    # Belt-and-suspenders: every row queued for cloud replication is boundary-clean.
    classifier = PayloadClassifier(
        max_summary_len=settings.max_summary_len,
        max_structured_text_len=settings.max_structured_text_len,
    )
    for op in store.replica.pending():
        for verdict in classifier.classify(op.values):
            assert verdict.verdict is Verdict.ALLOWED, (op.table, verdict)
