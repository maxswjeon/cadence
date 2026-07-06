"""Tests for the S0.0 capture harness + bootstrap labeling spike.

Exercises `cadence.spikes.s0_0` against synthetic/fixture data only (no live device,
no real user data — see `.omc/plans/cadence-phase0-spikes.md`).
"""

from __future__ import annotations

import json

from cadence.spikes.s0_0.harness import CaptureHarness, SignalLog
from cadence.spikes.s0_0.labeling import GoldLabel, LabelStore
from cadence.spikes.s0_0.sample import build_bootstrap_sample
from cadence.stores.nas import NASStore


class _StaticAdapter:
    """Minimal stand-in exposing the `.emit()` contract, for harness-only tests."""

    def __init__(self, events):
        self._events = events

    def emit(self):
        return iter(self._events)


def _event(event_id, occurred_at, source="test", **kw):
    from cadence.adapters.base import AcquisitionTier, Event

    return Event(
        event_id=event_id,
        source=source,
        account_ref="acct",
        acquisition_tier=AcquisitionTier.MANUAL,
        kind=kw.pop("kind", "test.item"),
        occurred_at=occurred_at,
        **kw,
    ).with_dedupe_id()


# --------------------------------------------------------------------------- #
# CaptureHarness / SignalLog
# --------------------------------------------------------------------------- #


def test_harness_orders_events_across_sources_by_time():
    from datetime import UTC, datetime

    early = _event("e-early", datetime(2026, 1, 1, tzinfo=UTC))
    mid = _event("e-mid", datetime(2026, 6, 1, tzinfo=UTC))
    late = _event("e-late", datetime(2026, 12, 1, tzinfo=UTC))

    harness = CaptureHarness()
    harness.add_source(_StaticAdapter([late, early]))
    harness.add_source([mid])
    log = harness.capture()

    assert isinstance(log, SignalLog)
    assert [e.event_id for e in log] == ["e-early", "e-mid", "e-late"]
    assert len(log) == 3


def test_signal_log_by_source_and_by_dedupe_id():
    from datetime import UTC, datetime

    a = _event("a", datetime(2026, 1, 1, tzinfo=UTC), source="github")
    b = _event("b", datetime(2026, 1, 2, tzinfo=UTC), source="email")

    harness = CaptureHarness()
    harness.add_source([a, b])
    log = harness.capture()

    assert [e.event_id for e in log.by_source("github")] == ["a"]
    by_dedupe = log.by_dedupe_id()
    assert set(by_dedupe) == {a.dedupe_id, b.dedupe_id}


def test_harness_reuses_real_adapter_fixtures(settings):
    """The harness accumulates real Events from the existing reference adapters."""
    from pathlib import Path

    from cadence.adapters.github import GitHubAdapter

    nas = NASStore(settings)
    fixtures_dir = Path(__file__).parent / "fixtures"
    adapter = GitHubAdapter(
        "octocat", nas=nas, fixture_path=fixtures_dir / "github" / "events.json"
    )

    harness = CaptureHarness()
    harness.add_source(adapter)
    log = harness.capture()

    assert len(log) == 3
    assert all(e.source == "github" for e in log)


# --------------------------------------------------------------------------- #
# GoldLabel / LabelStore
# --------------------------------------------------------------------------- #


def test_gold_label_json_roundtrip():
    from datetime import UTC, datetime

    label = GoldLabel(
        dedupe_id="d1",
        has_deadline=True,
        due_at=datetime(2026, 7, 10, tzinfo=UTC),
        origin="explicit",
        divergence_expected=True,
        priority=1,
        notes="hand-reviewed",
    )
    restored = GoldLabel.from_json(json.loads(json.dumps(label.to_json())))
    assert restored == label


def test_label_store_label_event_requires_dedupe_id():
    from cadence.adapters.base import Event

    event = Event(event_id="x", source="s", account_ref="a", kind="k")
    store = LabelStore()
    try:
        store.label_event(event, has_deadline=False)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for an event with no dedupe_id")


def test_label_store_save_load_roundtrip(tmp_path):
    from datetime import UTC, datetime

    event = _event("e1", datetime(2026, 1, 1, tzinfo=UTC))
    store = LabelStore()
    store.label_event(event, has_deadline=True, due_at=datetime(2026, 7, 1, tzinfo=UTC))

    path = tmp_path / "labels.json"
    store.save(path)
    loaded = LabelStore.load(path)

    assert len(loaded) == 1
    assert loaded.get(event.dedupe_id).has_deadline is True


def test_label_store_as_labeled_sample_drops_unmatched_labels():
    from datetime import UTC, datetime

    event = _event("e1", datetime(2026, 1, 1, tzinfo=UTC))
    harness = CaptureHarness()
    harness.add_source([event])
    log = harness.capture()

    store = LabelStore()
    store.label_event(event, has_deadline=False)
    store.add(GoldLabel(dedupe_id="ghost-not-captured", has_deadline=True))

    sample = store.as_labeled_sample(log)
    pairs = sample.pairs()
    assert len(pairs) == 1
    assert pairs[0][0].event_id == "e1"


def test_bootstrap_cli_consolidates_batch(tmp_path):
    from cadence.spikes.s0_0 import labeling

    labels_in = tmp_path / "batch.json"
    labels_in.write_text(
        json.dumps(
            [
                {"dedupe_id": "d1", "has_deadline": True, "due_at": "2026-07-10T00:00:00+00:00"},
                {"dedupe_id": "d2", "has_deadline": False},
            ]
        )
    )
    labels_out = tmp_path / "out.json"

    store = labeling.main([str(labels_in), str(labels_out)])
    assert len(store) == 2
    assert labels_out.exists()

    reloaded = LabelStore.load(labels_out)
    assert reloaded.get("d1").has_deadline is True
    assert reloaded.get("d2").has_deadline is False


# --------------------------------------------------------------------------- #
# Synthetic bootstrap sample (harness + labeling wired together)
# --------------------------------------------------------------------------- #


def test_build_bootstrap_sample_covers_expected_edge_cases(settings):
    nas = NASStore(settings)
    signal_log, store = build_bootstrap_sample(nas)

    # 3 github + 2 email + 2 gcal + 5 synthetic
    assert len(signal_log) == 12
    assert len(store) == 12

    sample = store.as_labeled_sample(signal_log)
    pairs = sample.pairs()
    assert len(pairs) == 12

    labels_by_event_id = {event.event_id: label for event, label in pairs}
    # explicit true positive from a real fixture (milestone_due_on)
    assert labels_by_event_id["github:octocat/hello-world#42"].has_deadline is True
    # the precision trap and the relative-weekday recall gap are both present
    assert labels_by_event_id["synthetic:fp-trap"].has_deadline is False
    assert labels_by_event_id["synthetic:fn-relative-weekday"].has_deadline is True
    assert labels_by_event_id["synthetic:divergence"].divergence_expected is True
