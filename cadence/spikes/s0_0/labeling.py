"""Bootstrap gold-labeling schema + helper (spike S0.0).

This is a **one-time bootstrap** step to produce a small labeled sample for the S0.2
calibration harness — it is explicitly NOT an ongoing task-list/labeling workflow (the
no-list mandate: Cadence does not maintain a persistent task list, and neither does
this). A human attaches a handful of gold labels once, against a captured
:class:`~cadence.spikes.s0_0.harness.SignalLog`; the resulting :class:`LabeledSample` is
a static artifact consumed by S0.2, not a live queue that grows over time.

:class:`LabelStore` also exposes a thin batch-apply CLI (:func:`main`) for that
one-time step: a reviewer produces a JSON file of :class:`GoldLabel`-shaped dicts (by
hand, or by exporting a signal log and annotating it) and this consolidates them into a
single :class:`LabelStore` JSON snapshot.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cadence.adapters.base import Event

from .harness import SignalLog


def _now() -> datetime:
    return datetime.now(tz=UTC)


@dataclass
class GoldLabel:
    """One human-attached ground-truth label for a single captured Event.

    ``priority`` is part of the bootstrap schema (``Task.priority`` exists in
    ``cadence.stores.models``) but **no priority extractor exists yet** anywhere in the
    codebase — S0.2 leaves priority evaluation as a documented stub rather than
    fabricating a verdict for a class that isn't implemented. It is captured here so a
    future priority extractor's calibration can reuse this same bootstrap sample.
    """

    dedupe_id: str
    has_deadline: bool
    due_at: datetime | None = None
    origin: str | None = None  # "explicit" | "inferred" | None, matches DeadlineCandidate.origin
    divergence_expected: bool = False
    priority: int | None = None  # bootstrap-schema only; see class docstring
    notes: str = ""
    labeled_by: str = "bootstrap"
    labeled_at: datetime = field(default_factory=_now)

    def to_json(self) -> dict[str, Any]:
        return {
            "dedupe_id": self.dedupe_id,
            "has_deadline": self.has_deadline,
            "due_at": self.due_at.isoformat() if self.due_at else None,
            "origin": self.origin,
            "divergence_expected": self.divergence_expected,
            "priority": self.priority,
            "notes": self.notes,
            "labeled_by": self.labeled_by,
            "labeled_at": self.labeled_at.isoformat(),
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> GoldLabel:
        due_at = datetime.fromisoformat(data["due_at"]) if data.get("due_at") else None
        labeled_at = (
            datetime.fromisoformat(data["labeled_at"]) if data.get("labeled_at") else _now()
        )
        return cls(
            dedupe_id=data["dedupe_id"],
            has_deadline=data["has_deadline"],
            due_at=due_at,
            origin=data.get("origin"),
            divergence_expected=data.get("divergence_expected", False),
            priority=data.get("priority"),
            notes=data.get("notes", ""),
            labeled_by=data.get("labeled_by", "bootstrap"),
            labeled_at=labeled_at,
        )


@dataclass
class LabeledSample:
    """A :class:`~cadence.spikes.s0_0.harness.SignalLog` joined with its gold labels —
    the artifact S0.2 evaluates against."""

    events_by_dedupe_id: dict[str, Event]
    labels: dict[str, GoldLabel]

    def pairs(self) -> list[tuple[Event, GoldLabel]]:
        """Labeled (event, gold) pairs; a label with no matching captured event is
        dropped rather than raising — the signal log and label batch may come from
        different runs."""
        return [
            (self.events_by_dedupe_id[dedupe_id], label)
            for dedupe_id, label in self.labels.items()
            if dedupe_id in self.events_by_dedupe_id
        ]

    def __len__(self) -> int:
        return len(self.pairs())


class LabelStore:
    """In-memory (optionally file-backed) collection of :class:`GoldLabel` rows keyed
    by ``dedupe_id``.

    This is deliberately not a task list: it holds a fixed, one-time set of bootstrap
    labels for calibration, not ongoing/evolving task state. ``save``/``load``
    round-trip a static snapshot; there is no update-forever workflow.
    """

    def __init__(self) -> None:
        self._labels: dict[str, GoldLabel] = {}

    def __len__(self) -> int:
        return len(self._labels)

    def add(self, label: GoldLabel) -> None:
        self._labels[label.dedupe_id] = label

    def label_event(self, event: Event, *, has_deadline: bool, **fields: Any) -> GoldLabel:
        """Convenience: build + add a :class:`GoldLabel` keyed off an already
        dedupe-tagged Event (see ``Event.with_dedupe_id``)."""
        if not event.dedupe_id:
            raise ValueError("event must carry a dedupe_id (call with_dedupe_id() first)")
        label = GoldLabel(dedupe_id=event.dedupe_id, has_deadline=has_deadline, **fields)
        self.add(label)
        return label

    def get(self, dedupe_id: str) -> GoldLabel | None:
        return self._labels.get(dedupe_id)

    def values(self) -> list[GoldLabel]:
        return list(self._labels.values())

    def save(self, path: Path) -> None:
        path.write_text(json.dumps([label.to_json() for label in self._labels.values()], indent=2))

    @classmethod
    def load(cls, path: Path) -> LabelStore:
        store = cls()
        for raw in json.loads(path.read_text()):
            store.add(GoldLabel.from_json(raw))
        return store

    def as_labeled_sample(self, signal_log: SignalLog) -> LabeledSample:
        """Join this store's labels against a captured
        :class:`~cadence.spikes.s0_0.harness.SignalLog`."""
        return LabeledSample(
            events_by_dedupe_id=signal_log.by_dedupe_id(), labels=dict(self._labels)
        )


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cadence-bootstrap-label",
        description=(
            "One-time bootstrap gold-labeling: consolidate a batch of hand-reviewed "
            "GoldLabel dicts into a single LabelStore JSON snapshot for the S0.2 "
            "calibration harness. This is a bootstrap step, not an ongoing task-list "
            "workflow."
        ),
    )
    parser.add_argument(
        "labels_in", type=Path, help="JSON file: a list of GoldLabel-shaped dicts"
    )
    parser.add_argument(
        "labels_out", type=Path, help="output path for the consolidated LabelStore JSON"
    )
    return parser


def main(argv: list[str] | None = None) -> LabelStore:
    """Batch-apply CLI entry point: consolidate ``labels_in`` into ``labels_out``."""
    args = _build_arg_parser().parse_args(argv)
    store = LabelStore()
    for raw in json.loads(args.labels_in.read_text()):
        store.add(GoldLabel.from_json(raw))
    store.save(args.labels_out)
    return store


if __name__ == "__main__":
    main()


__all__ = ["GoldLabel", "LabeledSample", "LabelStore", "main"]
