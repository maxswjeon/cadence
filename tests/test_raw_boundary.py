"""Tests for the D1 raw-boundary payload classifier."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from cadence.stores.raw_boundary import (
    PayloadClassifier,
    RawBoundaryViolation,
    Verdict,
    lint_columns,
)


@pytest.fixture
def classifier() -> PayloadClassifier:
    return PayloadClassifier(max_summary_len=500, max_structured_text_len=280)


@pytest.mark.parametrize(
    "name,value",
    [
        ("source_event_id", "gh-issue-42"),
        ("confidence_value", 0.87),
        ("content_hash", "a" * 64),
        ("summary", "Short non-verbatim summary of an event."),
        ("starts_at", datetime(2026, 7, 6, tzinfo=UTC)),
        ("created_at", "2026-07-06T18:52:00+00:00"),
        ("priority", 3),
        ("source_event_ids", ["e1", "e2"]),
        ("latitude", 37.5),
    ],
)
def test_structured_fields_allowed(classifier: PayloadClassifier, name, value) -> None:
    assert classifier.classify_field(name, value).verdict is Verdict.ALLOWED


@pytest.mark.parametrize(
    "name,value",
    [
        ("message_text", "hey are we still on for lunch?"),
        ("email_body", "Dear sir, ..."),
        ("transcript", "so then he said ..."),
        ("raw_ocr", "scanned text"),
        ("screenshot", b"\x89PNG..."),
        ("account_number", "1002-345-678901"),
        ("password", "hunter2"),
        ("api_token", "ghp_xxx"),
    ],
)
def test_raw_named_or_raw_valued_rejected(classifier: PayloadClassifier, name, value) -> None:
    assert classifier.classify_field(name, value).verdict is Verdict.REJECTED


def test_account_number_in_any_field_rejected(classifier: PayloadClassifier) -> None:
    # Even an innocuously-named field cannot smuggle an account number.
    verdict = classifier.classify_field("note_summary", "acct 1002 3456 7890 12")
    assert verdict.verdict is Verdict.REJECTED


def test_overlong_freetext_rejected(classifier: PayloadClassifier) -> None:
    verdict = classifier.classify_field("object_label", "x" * 300)
    assert verdict.verdict is Verdict.REJECTED


def test_summary_gets_larger_budget_but_bounded(classifier: PayloadClassifier) -> None:
    assert classifier.classify_field("summary", "x" * 400).verdict is Verdict.ALLOWED
    assert classifier.classify_field("summary", "x" * 600).verdict is Verdict.REJECTED


def test_enforce_raises_on_violation(classifier: PayloadClassifier) -> None:
    with pytest.raises(RawBoundaryViolation) as exc:
        classifier.enforce({"ok": "fine", "message_text": "verbatim raw"}, tier="D1")
    assert exc.value.field == "message_text"
    assert exc.value.tier == "D1"


def test_enforce_passes_clean_payload(classifier: PayloadClassifier) -> None:
    classifier.enforce(
        {
            "source_event_id": "e1",
            "confidence_value": 0.9,
            "raw_evidence_hash": "b" * 64,
            "summary": "a short summary",
        }
    )


def test_hex_digest_allowed(classifier: PayloadClassifier) -> None:
    assert classifier.classify_field("raw_evidence_hash", "f" * 64).verdict is Verdict.ALLOWED


def test_column_lint_flags_raw_named_columns() -> None:
    bad = lint_columns("some_table", ["id", "created_at", "message_body", "summary"])
    assert bad == ["message_body"]
