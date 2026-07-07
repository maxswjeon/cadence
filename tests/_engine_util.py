"""Synthetic-data helpers for the engine tests (not a test module itself).

Every helper writes real D1 rows through :class:`~cadence.stores.d1.D1Store` (so the
raw boundary is exercised on the write path) with **explicit** timestamps, so the
engine's injected-clock logic is fully deterministic.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from cadence.stores.d1 import D1Store
from cadence.stores.models import Deadline, Fact, Task


def activity(
    store: D1Store,
    target: str,
    ts: datetime,
    *,
    kind: str = "device.active_window",
) -> Fact:
    """Write one device-activity fact observed at ``ts`` (target in ``object_label``)."""
    fact = Fact(
        kind=kind,
        subject_type="device",
        subject_id="laptop",
        predicate="active",
        object_label=target,
        confidence_value=0.9,
        confidence_type="observed",
        source_event_ids=[f"evt-{uuid.uuid4().hex[:8]}"],
        dedupe_key=uuid.uuid4().hex,
    )
    fact.created_at = ts
    store.write(fact)
    return fact


def spread_activity(
    store: D1Store,
    target: str,
    end: datetime,
    *,
    count: int,
    step_seconds: int,
    kind: str = "device.active_window",
) -> list[Fact]:
    """Write ``count`` same-target facts ending at ``end``, one every ``step_seconds``."""
    from datetime import timedelta

    facts = []
    for i in range(count):
        ts = end - timedelta(seconds=step_seconds * (count - 1 - i))
        facts.append(activity(store, target, ts, kind=kind))
    return facts


def battery(
    store: D1Store,
    level: int,
    ts: datetime,
    *,
    device: str = "phone",
    summary: str | None = None,
) -> Fact:
    """Write a ``device.power`` telemetry fact with battery percent in ``object_label``."""
    fact = Fact(
        kind="device.power",
        subject_type="device",
        subject_id=device,
        predicate="battery_level",
        object_label=str(level),
        confidence_value=1.0,
        confidence_type="observed",
        source_event_ids=[f"pwr-{uuid.uuid4().hex[:8]}"],
        summary=summary,
        dedupe_key=uuid.uuid4().hex,
    )
    fact.created_at = ts
    store.write(fact)
    return fact


def make_task(
    store: D1Store,
    title: str,
    *,
    priority: int | None = None,
    status: str = "open",
    summary: str | None = None,
    source_event_ids: list[str] | None = None,
) -> Task:
    """Write an open (by default) task row."""
    task = Task(
        title=title,
        status=status,
        priority=priority,
        summary=summary,
        source_event_ids=source_event_ids or [f"task-{uuid.uuid4().hex[:8]}"],
    )
    store.write(task)
    return task


def make_deadline(
    store: D1Store,
    task_id: str | None,
    due_at: datetime,
    *,
    origin: str = "explicit",
    source_event_ids: list[str] | None = None,
    summary: str | None = None,
) -> Deadline:
    """Write a deadline row (optionally attached to ``task_id``)."""
    dl = Deadline(
        task_id=task_id,
        due_at=due_at,
        origin=origin,
        source_event_ids=source_event_ids or [f"dl-{uuid.uuid4().hex[:8]}"],
        summary=summary,
    )
    store.write(dl)
    return dl
