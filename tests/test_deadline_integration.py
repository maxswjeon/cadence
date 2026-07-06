"""End-to-end proof that RuleDeadlineExtractor works against a real reference adapter.

Builds a GitHub-style event through the actual :class:`GitHubAdapter` (an inline
in-memory record — not touching the shared ``tests/fixtures/github/`` fixtures worker-2
owns) and runs it through the real :class:`IngestPipeline`, asserting the explicit
``due_on`` milestone date lands as a correctly-provenanced ``deadline`` row.
"""

from __future__ import annotations

from datetime import UTC, datetime

from cadence.adapters.github import GitHubAdapter
from cadence.brain.deadlines import RuleDeadlineExtractor
from cadence.ingest.pipeline import IngestPipeline
from cadence.stores.models import Deadline
from cadence.stores.nas import NASStore


def _issue_record(**overrides) -> dict:
    record = {
        "type": "issue",
        "repo": "acme/widgets",
        "number": 42,
        "title": "Ship the widget",
        "state": "open",
        "labels": ["priority"],
        "assignees": ["octocat"],
        "author": "octocat",
        "updated_at": "2026-07-01T00:00:00Z",
        "milestone_due_on": "2026-07-15",
        "html_url": "https://github.com/acme/widgets/issues/42",
    }
    record.update(overrides)
    return record


def test_github_issue_due_on_becomes_explicit_deadline_row(settings, store) -> None:
    adapter = GitHubAdapter(
        "octocat",
        nas=NASStore(settings),
        records=[_issue_record()],
    )
    events = list(adapter.emit())
    assert len(events) == 1
    event = events[0]

    # Sanity-check the real adapter output before trusting the extractor with it.
    assert event.kind == "github.issue"
    assert event.structured["due_on"] == "2026-07-15"

    pipe = IngestPipeline(store, deadline_extractor=RuleDeadlineExtractor())
    result = pipe.ingest(event)
    assert result.deadlines_created == 1

    with store.session() as session:
        dl = session.query(Deadline).one()
        assert dl.origin == "explicit"
        assert not dl.divergence_flag
        assert dl.due_at.replace(tzinfo=UTC) == datetime(2026, 7, 15, tzinfo=UTC)
        assert dl.source_event_ids == [event.event_id]


def test_github_issue_without_milestone_due_date_yields_no_deadline(settings, store) -> None:
    adapter = GitHubAdapter(
        "octocat",
        nas=NASStore(settings),
        records=[_issue_record(milestone_due_on=None, number=43)],
    )
    event = next(iter(adapter.emit()))
    assert event.structured["due_on"] is None

    pipe = IngestPipeline(store, deadline_extractor=RuleDeadlineExtractor())
    result = pipe.ingest(event)
    assert result.deadlines_created == 0
    with store.session() as session:
        assert session.query(Deadline).count() == 0
