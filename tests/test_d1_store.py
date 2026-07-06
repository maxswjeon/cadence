"""Tests for the local-canonical D1 store + Cloudflare-D1 replica stub."""

from __future__ import annotations

import pytest

from cadence.obs.alarms import get_alarm_sink
from cadence.stores.d1 import D1Store
from cadence.stores.models import CalendarEvent, Fact, SourceAccount
from cadence.stores.raw_boundary import RawBoundaryViolation


def test_write_persists_and_enqueues_replica(store: D1Store) -> None:
    acct = SourceAccount(provider="github", account_ref="octocat")
    store.write(acct)
    assert store.replica.queue_depth == 1
    op = store.replica.pending()[0]
    assert op.table == "source_account"
    assert op.values["provider"] == "github"


def test_write_all_batches(store: D1Store) -> None:
    rows = [SourceAccount(provider="p", account_ref=f"a{i}") for i in range(3)]
    store.write_all(rows)
    assert store.replica.queue_depth == 3


def test_local_canonical_readback(store: D1Store) -> None:
    ev = CalendarEvent(title="Standup", status="confirmed")
    store.write(ev)
    with store.session() as session:
        got = session.get(CalendarEvent, ev.id)
        assert got is not None and got.title == "Standup"


def test_raw_to_d1_violation_is_blocked_and_alarmed(store: D1Store) -> None:
    """A verbatim-carrying row must be rejected before it reaches D1 (local OR replica)."""
    fact = Fact(kind="note", dedupe_key="k1", summary="acct 1002 3456 7890 12")
    with pytest.raises(RawBoundaryViolation):
        store.write(fact)
    # Nothing replicated, and the alarm fired.
    assert store.replica.queue_depth == 0
    assert get_alarm_sink().count("raw_to_cloud_violation") == 1


def test_replica_flush_is_async(store: D1Store) -> None:
    import asyncio

    store.write(SourceAccount(provider="p", account_ref="a"))
    drained = asyncio.run(store.replica.flush())
    assert drained == 1
    assert store.replica.queue_depth == 0
