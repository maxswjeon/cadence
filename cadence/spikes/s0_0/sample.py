"""Synthetic bootstrap-labeled sample (spike S0.0).

Builds a small :class:`~cadence.spikes.s0_0.labeling.LabeledSample` by running the
existing reference adapters over their recorded fixtures
(``tests/fixtures/{github,email,google_calendar}/``) plus a handful of hand-built
synthetic Events covering cases the fixtures don't exercise: a false-positive keyword
trap, a missed-relative-date false negative, and an explicit/inferred divergence. The
gold labels below ARE the one-time bootstrap labeling this spike calls for — attached
once, by hand, to build this fixture — not an ongoing labeling queue.

**This sample is synthetic and tiny.** It exists to prove the S0.0/S0.2 harnesses run
end-to-end, not to produce a real calibration verdict — see
``.omc/research/spikes/s0_2.md`` for why real thresholds need a real captured signal
log. Only reads the reference adapters and fixtures; does not modify them.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cadence.adapters.base import AcquisitionTier, Event
from cadence.adapters.email import EmailAdapter
from cadence.adapters.gcal import GoogleCalendarAdapter
from cadence.adapters.github import GitHubAdapter
from cadence.stores.nas import NASStore

from .harness import CaptureHarness, SignalLog
from .labeling import LabelStore

#: Reused read-only from the reference-adapter test suite (see AGENTS.md's adapter
#: section) — this spike does not modify those fixtures.
_FIXTURES_DIR = Path(__file__).resolve().parents[3] / "tests" / "fixtures"


def _synthetic_events() -> list[Event]:
    """Hand-built edge cases the fixtures don't cover, each dedupe-tagged like a real
    adapter's ``emit()`` would."""
    events = [
        Event(
            event_id="synthetic:fp-trap",
            source="synthetic",
            account_ref="spike",
            acquisition_tier=AcquisitionTier.MANUAL,
            kind="synthetic.note",
            occurred_at=datetime(2026, 1, 2, tzinfo=UTC),
            summary="Shipped release on 2026-01-01, nothing else is due right now",
        ),
        Event(
            event_id="synthetic:fn-relative-weekday",
            source="synthetic",
            account_ref="spike",
            acquisition_tier=AcquisitionTier.MANUAL,
            kind="synthetic.note",
            occurred_at=datetime(2026, 7, 6, tzinfo=UTC),
            summary="Please wrap this up before next Friday",
        ),
        Event(
            event_id="synthetic:tn-chatter",
            source="synthetic",
            account_ref="spike",
            acquisition_tier=AcquisitionTier.MANUAL,
            kind="synthetic.note",
            occurred_at=datetime(2026, 7, 6, tzinfo=UTC),
            summary="Just checking in, no action needed on either side",
        ),
        Event(
            event_id="synthetic:tp-inferred-dminus",
            source="synthetic",
            account_ref="spike",
            acquisition_tier=AcquisitionTier.MANUAL,
            kind="synthetic.note",
            occurred_at=datetime(2026, 7, 1, tzinfo=UTC),
            summary="Assignment D-3",
        ),
        Event(
            event_id="synthetic:divergence",
            source="synthetic",
            account_ref="spike",
            acquisition_tier=AcquisitionTier.MANUAL,
            kind="synthetic.note",
            occurred_at=datetime(2026, 7, 1, tzinfo=UTC),
            structured={"due_at": "2026-07-10T00:00:00+00:00"},
            summary="but the real deadline is due 2026-07-20",
        ),
    ]
    return [e.with_dedupe_id() for e in events]


def _fixture_events(nas: NASStore) -> list[Event]:
    github = GitHubAdapter(
        "octocat", nas=nas, fixture_path=_FIXTURES_DIR / "github" / "events.json"
    )
    email = EmailAdapter("me", nas=nas, fixture_path=_FIXTURES_DIR / "email" / "messages.json")
    gcal = GoogleCalendarAdapter(
        "alice", nas=nas, fixture_path=_FIXTURES_DIR / "google_calendar" / "events.json"
    )
    events: list[Event] = []
    for adapter in (github, email, gcal):
        events.extend(adapter.emit())
    return events


def build_bootstrap_sample(nas: NASStore) -> tuple[SignalLog, LabelStore]:
    """Capture fixtures + synthetic edge cases and attach the bootstrap gold labels.

    Returns the :class:`~cadence.spikes.s0_0.harness.SignalLog` (for inspecting the
    harness's own output) and the :class:`~cadence.spikes.s0_0.labeling.LabelStore`;
    join them with :meth:`LabelStore.as_labeled_sample` to get what S0.2 evaluates.
    """
    harness = CaptureHarness()
    harness.add_source(_fixture_events(nas))
    harness.add_source(_synthetic_events())
    signal_log = harness.capture()

    events_by_id = {e.event_id: e for e in signal_log}
    store = LabelStore()

    def _label(event_id: str, **fields: Any) -> None:
        store.label_event(events_by_id[event_id], **fields)

    # --- GitHub fixture ------------------------------------------------------
    _label(
        "github:octocat/hello-world#42",
        has_deadline=True,
        due_at=datetime(2026, 7, 15, tzinfo=UTC),
        origin="explicit",
        notes="milestone_due_on -> explicit due date",
    )
    _label(
        "github:octocat/hello-world#43",
        has_deadline=False,
        notes="no due date, no deadline phrase",
    )
    _label(
        "github:octocat/hello-world#43:review:hubot",
        has_deadline=False,
        notes="review request, no deadline phrase",
    )

    # --- Email fixture -------------------------------------------------------
    _label(
        "msg-100",
        has_deadline=True,
        notes=(
            "subject says 'by Friday' -- a real implied deadline, but the rule "
            "extractor cannot parse a relative weekday; expected FALSE NEGATIVE"
        ),
    )
    _label("msg-101", has_deadline=False, notes="newsletter, no deadline phrase")

    # --- Google Calendar fixture ---------------------------------------------
    _label("evt_1", has_deadline=False, notes="team sync, no due date")
    _label("evt_2", has_deadline=False, notes="all-day offsite, no due date")

    # --- Synthetic edge cases -------------------------------------------------
    _label(
        "synthetic:fp-trap",
        has_deadline=False,
        notes=(
            "contains the keyword 'due' near an unrelated date; expected FALSE "
            "POSITIVE from the rule extractor (precision trap)"
        ),
    )
    _label(
        "synthetic:fn-relative-weekday",
        has_deadline=True,
        notes=(
            "'before next Friday' -- real deadline, unparseable relative weekday; "
            "expected FALSE NEGATIVE"
        ),
    )
    _label("synthetic:tn-chatter", has_deadline=False, notes="no deadline signal at all")
    _label(
        "synthetic:tp-inferred-dminus",
        has_deadline=True,
        origin="inferred",
        due_at=datetime(2026, 7, 4, tzinfo=UTC),
        notes="D-3 countdown relative to occurred_at",
    )
    _label(
        "synthetic:divergence",
        has_deadline=True,
        origin="explicit",
        due_at=datetime(2026, 7, 10, tzinfo=UTC),
        divergence_expected=True,
        notes="explicit 2026-07-10 vs inferred 2026-07-20 -- divergence must be flagged",
    )

    return signal_log, store


__all__ = ["build_bootstrap_sample"]
