"""Tests for the rule-based DeadlineExtractor: explicit, inferred, and divergence."""

from __future__ import annotations

from datetime import UTC, datetime

from cadence.adapters.base import AcquisitionTier, Event
from cadence.brain.deadlines import DeadlineCandidate, RuleDeadlineExtractor
from cadence.ingest.pipeline import IngestPipeline
from cadence.stores.models import Deadline


def _event(event_id="e1", **kw) -> Event:
    return Event(
        event_id=event_id,
        source="test",
        account_ref="acct",
        kind=kw.pop("kind", "calendar.event"),
        acquisition_tier=AcquisitionTier.MANUAL,
        **kw,
    )


# --------------------------------------------------------------------------- #
# Explicit extraction (Event.structured)
# --------------------------------------------------------------------------- #


def test_explicit_due_at_from_structured() -> None:
    event = _event(structured={"due_at": "2026-07-10T17:00:00+00:00"})
    candidates = RuleDeadlineExtractor().extract(event)
    assert len(candidates) == 1
    cand = candidates[0]
    assert cand.origin == "explicit"
    assert cand.due_at == datetime(2026, 7, 10, 17, 0, tzinfo=UTC)
    assert cand.confidence_value == 1.0
    assert not cand.divergence_flag


def test_explicit_deadline_key_accepts_datetime_value() -> None:
    event = _event(structured={"deadline": datetime(2026, 8, 1, tzinfo=UTC)})
    candidates = RuleDeadlineExtractor().extract(event)
    assert len(candidates) == 1
    assert candidates[0].due_at == datetime(2026, 8, 1, tzinfo=UTC)


def test_no_candidates_when_nothing_found() -> None:
    event = _event(summary="just chatting, no dates here")
    assert RuleDeadlineExtractor().extract(event) == []


# --------------------------------------------------------------------------- #
# Inferred extraction (Event.summary heuristics)
# --------------------------------------------------------------------------- #


def test_inferred_due_phrase_iso_date() -> None:
    event = _event(kind="email.message", summary="Rent payment due 2026-07-15")
    candidates = RuleDeadlineExtractor().extract(event)
    assert len(candidates) == 1
    cand = candidates[0]
    assert cand.origin == "inferred"
    assert cand.due_at == datetime(2026, 7, 15, 23, 59, tzinfo=UTC)
    assert cand.confidence_type == "heuristic"


def test_inferred_by_phrase_slash_date_uses_reference_year() -> None:
    event = _event(
        kind="email.message",
        summary="Please submit the report by 7/20",
        occurred_at=datetime(2026, 7, 1, tzinfo=UTC),
    )
    candidates = RuleDeadlineExtractor().extract(event)
    assert len(candidates) == 1
    assert candidates[0].due_at == datetime(2026, 7, 20, 23, 59, tzinfo=UTC)


def test_inferred_korean_deadline_phrase() -> None:
    event = _event(
        kind="email.message",
        summary="마감 7월 10일까지 제출",
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    candidates = RuleDeadlineExtractor().extract(event)
    assert len(candidates) == 1
    assert candidates[0].due_at == datetime(2026, 7, 10, 23, 59, tzinfo=UTC)


def test_inferred_d_minus_countdown_relative_to_occurred_at() -> None:
    event = _event(
        kind="notification.wal",
        summary="Assignment D-3",
        occurred_at=datetime(2026, 7, 1, tzinfo=UTC),
    )
    candidates = RuleDeadlineExtractor().extract(event)
    assert len(candidates) == 1
    cand = candidates[0]
    assert cand.origin == "inferred"
    assert cand.due_at == datetime(2026, 7, 4, tzinfo=UTC)


def test_bill_due_phrase_is_recognized() -> None:
    event = _event(kind="email.message", summary="Electric bill due 2026-09-01")
    candidates = RuleDeadlineExtractor().extract(event)
    assert len(candidates) == 1
    assert candidates[0].due_at == datetime(2026, 9, 1, 23, 59, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# Relative-weekday inference ("by Friday", "next Monday", ...)
# --------------------------------------------------------------------------- #


def test_inferred_by_weekday_resolves_to_next_occurrence() -> None:
    # 2026-07-01 is a Wednesday; the next Friday is 2026-07-03.
    event = _event(
        kind="email.message",
        summary="Please submit by Friday",
        occurred_at=datetime(2026, 7, 1, tzinfo=UTC),
    )
    candidates = RuleDeadlineExtractor().extract(event)
    assert len(candidates) == 1
    cand = candidates[0]
    assert cand.origin == "inferred"
    assert cand.confidence_type == "heuristic"
    assert cand.due_at == datetime(2026, 7, 3, 23, 59, tzinfo=UTC)


def test_inferred_due_weekday_bare_form() -> None:
    # 2026-07-01 is a Wednesday; "due Monday" means the upcoming Monday, 2026-07-06.
    event = _event(
        kind="email.message",
        summary="Report due Monday",
        occurred_at=datetime(2026, 7, 1, tzinfo=UTC),
    )
    candidates = RuleDeadlineExtractor().extract(event)
    assert len(candidates) == 1
    assert candidates[0].due_at == datetime(2026, 7, 6, 23, 59, tzinfo=UTC)


def test_inferred_next_weekday_skips_the_immediate_occurrence() -> None:
    # 2026-07-01 is a Wednesday; the immediate next Monday is 2026-07-06, but "next
    # Monday" means the one after that: 2026-07-13.
    event = _event(
        kind="email.message",
        summary="Submit by next Monday",
        occurred_at=datetime(2026, 7, 1, tzinfo=UTC),
    )
    candidates = RuleDeadlineExtractor().extract(event)
    assert len(candidates) == 1
    assert candidates[0].due_at == datetime(2026, 7, 13, 23, 59, tzinfo=UTC)


def test_inferred_weekday_same_day_as_reference_rolls_to_next_week() -> None:
    # 2026-07-01 is itself a Wednesday; "due Wednesday" on a Wednesday means the
    # *next* Wednesday (2026-07-08), not later today.
    event = _event(
        kind="email.message",
        summary="Renewal due Wednesday",
        occurred_at=datetime(2026, 7, 1, tzinfo=UTC),
    )
    candidates = RuleDeadlineExtractor().extract(event)
    assert len(candidates) == 1
    assert candidates[0].due_at == datetime(2026, 7, 8, 23, 59, tzinfo=UTC)


def test_inferred_this_weekday_matches_bare_weekday() -> None:
    # "this Friday" means the same nearest-upcoming Friday as bare "Friday".
    event = _event(
        kind="email.message",
        summary="submit by this Friday",
        occurred_at=datetime(2026, 7, 1, tzinfo=UTC),
    )
    candidates = RuleDeadlineExtractor().extract(event)
    assert len(candidates) == 1
    assert candidates[0].due_at == datetime(2026, 7, 3, 23, 59, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# Keyword-proximity: a date must be syntactically tied to the deadline phrase
# --------------------------------------------------------------------------- #


def test_bare_date_near_keyword_is_not_mistaken_for_a_deadline() -> None:
    event = _event(
        kind="email.message",
        summary="the meeting on March 3 was about the deadline process",
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert RuleDeadlineExtractor().extract(event) == []


def test_date_tied_to_keyword_still_extracts_despite_earlier_unrelated_date() -> None:
    # "deadline" appears first but isn't followed by a date; "due" later in the same
    # sentence is directly tied to the real date and must still be picked up.
    event = _event(
        kind="email.message",
        summary="the deadline process aside, the real deadline is due 2026-07-20",
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    candidates = RuleDeadlineExtractor().extract(event)
    assert len(candidates) == 1
    cand = candidates[0]
    assert cand.origin == "inferred"
    assert cand.due_at == datetime(2026, 7, 20, 23, 59, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# Bounded filler window: a few words of slack between keyword and date, but not
# an unbounded scan (see _tied_date_in_clause / _MAX_FILLER_WORDS)
# --------------------------------------------------------------------------- #


def test_inferred_keyword_with_filler_words_before_date() -> None:
    # "for the report is" (3 words) sits between "deadline" and the date -- within the
    # bounded filler window ("is" is a free connector, doesn't count against it).
    event = _event(
        kind="email.message",
        summary="Deadline for the report is 2026-07-10",
    )
    candidates = RuleDeadlineExtractor().extract(event)
    assert len(candidates) == 1
    cand = candidates[0]
    assert cand.origin == "inferred"
    assert cand.due_at == datetime(2026, 7, 10, 23, 59, tzinfo=UTC)


def test_inferred_keyword_with_filler_words_before_weekday() -> None:
    # "for X on" sits between "due" and "Friday" -- "on" is a free connector, "for" and
    # "X" are filler words, both within budget.
    event = _event(
        kind="email.message",
        summary="Report due for X on Friday",
        occurred_at=datetime(2026, 7, 1, tzinfo=UTC),  # Wednesday
    )
    candidates = RuleDeadlineExtractor().extract(event)
    assert len(candidates) == 1
    cand = candidates[0]
    assert cand.origin == "inferred"
    assert cand.due_at == datetime(2026, 7, 3, 23, 59, tzinfo=UTC)


def test_bare_date_near_keyword_still_not_mistaken_beyond_filler_budget() -> None:
    # Same known false-positive case as above -- "deadline" isn't followed by a date at
    # all (just "process", then end of sentence), so no bounded filler scan finds one.
    event = _event(
        kind="email.message",
        summary="the meeting on March 3 was about the deadline process",
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert RuleDeadlineExtractor().extract(event) == []


def test_unrelated_date_beyond_filler_budget_is_not_extracted() -> None:
    # The date sits more than _MAX_FILLER_WORDS words after the keyword, with no real
    # syntactic tie -- still a false-positive shape, must still yield nothing.
    event = _event(
        kind="email.message",
        summary=(
            "there is a deadline somewhere but nobody remembers exactly when so we "
            "shipped anyway on 2026-01-01"
        ),
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert RuleDeadlineExtractor().extract(event) == []


# --------------------------------------------------------------------------- #
# Explicit-over-inferred preference + divergence
# --------------------------------------------------------------------------- #


def test_explicit_preferred_when_inferred_agrees() -> None:
    event = _event(
        kind="calendar.event",
        structured={"due_at": "2026-07-10T09:00:00+00:00"},
        summary="Invoice due 2026-07-10",
    )
    candidates = RuleDeadlineExtractor().extract(event)
    assert len(candidates) == 1
    assert candidates[0].origin == "explicit"
    assert not candidates[0].divergence_flag


def test_divergence_flag_set_when_explicit_and_inferred_disagree() -> None:
    event = _event(
        kind="calendar.event",
        structured={"due_at": "2026-07-10T00:00:00+00:00"},
        summary="but the real deadline is due 2026-07-20",
    )
    candidates = RuleDeadlineExtractor().extract(event)
    assert len(candidates) == 2

    by_origin = {c.origin: c for c in candidates}
    assert set(by_origin) == {"explicit", "inferred"}
    assert by_origin["explicit"].due_at == datetime(2026, 7, 10, 0, 0, tzinfo=UTC)
    assert by_origin["inferred"].due_at == datetime(2026, 7, 20, 23, 59, tzinfo=UTC)
    assert by_origin["explicit"].divergence_flag
    assert by_origin["inferred"].divergence_flag


def test_llm_hook_candidates_flow_through_reconciliation() -> None:
    def hook(event: Event) -> list[DeadlineCandidate]:
        return [
            DeadlineCandidate(
                due_at=datetime(2026, 12, 25, tzinfo=UTC),
                origin="inferred",
                confidence_value=0.4,
                confidence_type="llm",
                source_event_ids=[event.event_id],
                summary="llm-inferred",
            )
        ]

    event = _event(
        kind="calendar.event",
        structured={"due_at": "2026-07-10T00:00:00+00:00"},
    )
    candidates = RuleDeadlineExtractor(llm_hook=hook).extract(event)
    assert len(candidates) == 2
    by_origin = {c.origin: c for c in candidates}
    assert by_origin["explicit"].divergence_flag
    assert by_origin["inferred"].confidence_type == "llm"
    assert by_origin["inferred"].divergence_flag


# --------------------------------------------------------------------------- #
# Wired into IngestPipeline
# --------------------------------------------------------------------------- #


def test_pipeline_writes_deadline_row_from_explicit_structured(store) -> None:
    pipe = IngestPipeline(store, deadline_extractor=RuleDeadlineExtractor())
    event = _event(
        kind="calendar.event",
        structured={"due_at": "2026-07-10T17:00:00+00:00"},
    )
    result = pipe.ingest(event)
    assert result.deadlines_created == 1

    with store.session() as session:
        dl = session.query(Deadline).one()
        assert dl.origin == "explicit"
        assert dl.due_at.replace(tzinfo=UTC) == datetime(2026, 7, 10, 17, 0, tzinfo=UTC)
        assert dl.source_event_ids == ["e1"]
        assert not dl.divergence_flag


def test_pipeline_writes_both_rows_on_divergence(store) -> None:
    pipe = IngestPipeline(store, deadline_extractor=RuleDeadlineExtractor())
    event = _event(
        kind="email.message",
        structured={"due_at": "2026-07-10T00:00:00+00:00"},
        summary="actually due 2026-07-25",
    )
    result = pipe.ingest(event)
    assert result.deadlines_created == 2

    with store.session() as session:
        rows = session.query(Deadline).all()
        origins = {row.origin for row in rows}
        assert origins == {"explicit", "inferred"}
        assert all(row.divergence_flag for row in rows)
