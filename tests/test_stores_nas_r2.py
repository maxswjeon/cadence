"""Tests for NAS + R2 blob stores and the tiering router (incl. raw-to-cloud violation)."""

from __future__ import annotations

import pytest

from cadence.obs.alarms import get_alarm_sink
from cadence.stores.nas import NASStore, sha256_hex
from cadence.stores.r2 import Artifact, R2Store, Tier, TieringRouter
from cadence.stores.raw_boundary import RawBoundaryViolation


def test_nas_put_get_roundtrip(settings) -> None:
    nas = NASStore(settings)
    data = b"raw evidence bytes"
    ref = nas.put(data)
    assert ref.hash == sha256_hex(data)
    assert nas.exists(ref)
    assert nas.get(ref) == data


def test_nas_is_content_addressed(settings) -> None:
    nas = NASStore(settings)
    a = nas.put(b"same")
    b = nas.put(b"same")
    assert a.id == b.id  # dedup by content hash


def test_r2_stores_derived_blob(settings) -> None:
    r2 = R2Store(settings)
    ref = r2.put_derived(b"a derived transcript blob")
    assert r2.exists(ref)
    assert r2.get(ref) == b"a derived transcript blob"


def test_router_routes_raw_to_nas_and_derived_to_r2(settings, store) -> None:
    router = TieringRouter(settings=settings, d1=store)
    raw_ref = router.route(Artifact(tier=Tier.RAW, data=b"raw audio"))
    assert router.nas.exists(raw_ref)
    derived_ref = router.route(Artifact(tier=Tier.DERIVED_BLOB, data=b"summary", derived=True))
    assert router.r2.exists(derived_ref)


def test_router_blocks_raw_to_cloud(settings) -> None:
    router = TieringRouter(settings=settings)
    with pytest.raises(RawBoundaryViolation):
        router.guard_cloud_target(Tier.RAW, "R2")
    assert get_alarm_sink().count("raw_to_cloud_violation") == 1


def test_router_structured_goes_through_d1(settings, store) -> None:
    from cadence.stores.models import Task

    router = TieringRouter(settings=settings, d1=store)
    task = Task(title="do the thing")
    router.route(Artifact(tier=Tier.STRUCTURED, instance=task))
    with store.session() as session:
        assert session.get(Task, task.id) is not None
