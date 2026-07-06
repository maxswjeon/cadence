"""Concurrency tests for the ingest pipeline and typed-row projection.

``IngestPipeline`` is shared across FastAPI's threadpool-served sync handlers, so its
mutable state (``_seen``, the ``WALBuffer``) must not interleave under concurrent
callers — verified here with real threads driving the same pipeline instance. Separately,
:class:`~cadence.brain.projection.ProjectionContext`'s get-or-create (source_account /
place) is exercised with real threads bypassing the pipeline's lock entirely, to prove
the atomic-upsert-on-IntegrityError path (not just the pipeline-level serialization)
prevents duplicate rows under a genuine race.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from cadence.adapters.base import AcquisitionTier, Event
from cadence.brain.projection import ProjectionContext
from cadence.config import Settings
from cadence.ingest.pipeline import IngestPipeline
from cadence.stores.d1 import D1Store
from cadence.stores.models import Fact, SourceAccount

_N = 16


@pytest.fixture
def file_store(settings: Settings) -> D1Store:
    """A **file-backed** SQLite store — real per-thread connections via the pool.

    Unlike the ``store`` fixture (in-memory ``sqlite://`` on a single shared
    :class:`~sqlalchemy.pool.StaticPool` connection), this gives each thread its own
    connection to the same on-disk file, matching how ``D1Store`` is actually used in
    production (see ``settings.d1_sqlalchemy_url``). A shared single connection doesn't
    model real concurrent access — SQLite only serializes correctly across genuinely
    separate connections to the same file — so the atomic-upsert-under-race assertions
    below need this fixture, not ``store``.
    """
    s = D1Store(settings)
    s.init_schema()
    return s


def _event(event_id: str, **kw) -> Event:
    return Event(
        event_id=event_id,
        source="github",
        account_ref="octocat",
        kind="github.issue",
        acquisition_tier=AcquisitionTier.OFFICIAL_API,
        summary="issue: fix bug",
        confidence=0.9,
        **kw,
    )


def test_concurrent_ingest_of_same_event_creates_exactly_one_fact(store) -> None:
    """N threads ingest the *same* event (same dedupe_id) concurrently."""
    pipe = IngestPipeline(store)

    with ThreadPoolExecutor(max_workers=_N) as pool:
        results = list(pool.map(lambda _: pipe.ingest(_event("same-event")), range(_N)))

    accepted = [r for r in results if r.accepted]
    duplicates = [r for r in results if r.duplicate]
    assert len(accepted) == 1
    assert len(duplicates) == _N - 1
    with store.session() as session:
        assert session.query(Fact).count() == 1


def test_concurrent_ingest_of_distinct_events_all_accepted_no_wal_corruption(store) -> None:
    """N threads ingest N *distinct* events concurrently: all land, none are lost."""
    pipe = IngestPipeline(store)

    with ThreadPoolExecutor(max_workers=_N) as pool:
        results = list(pool.map(lambda i: pipe.ingest(_event(f"e{i}")), range(_N)))

    assert all(r.accepted for r in results)
    assert len({r.dedupe_id for r in results}) == _N
    assert pipe.wal.depth == 0  # every append was drained; no leftover/corrupted state
    with store.session() as session:
        assert session.query(Fact).count() == _N


def test_concurrent_source_account_get_or_create_is_race_safe(file_store) -> None:
    """Bypass the pipeline lock entirely: hammer ProjectionContext directly.

    This is the scenario the atomic upsert (IntegrityError -> re-SELECT, relying on
    the DB unique constraint on (provider, account_ref)) protects against — multiple
    independent get-or-create calls racing past the initial SELECT before either has
    inserted.
    """
    ctx = ProjectionContext(file_store)
    event = _event("unused")  # only source/account_ref are read by source_account_id

    with ThreadPoolExecutor(max_workers=_N) as pool:
        ids = list(pool.map(lambda _: ctx.source_account_id(event), range(_N)))

    assert len(set(ids)) == 1
    with file_store.session() as session:
        rows = session.query(SourceAccount).filter_by(
            provider="github", account_ref="octocat"
        ).all()
        assert len(rows) == 1


def test_concurrent_place_get_or_create_is_race_safe(file_store) -> None:
    ctx = ProjectionContext(file_store)

    with ThreadPoolExecutor(max_workers=_N) as pool:
        ids = list(pool.map(lambda _: ctx.place_id("HQ"), range(_N)))

    assert len(set(ids)) == 1
