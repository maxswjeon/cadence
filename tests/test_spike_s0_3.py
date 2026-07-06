"""S0.3 chaos test — failover/fence real pass/fail (Decision C / AC-6).

The headline test (:func:`test_chaos_failover_holds_all_properties`) drives the full
Normal -> NAS-down -> cloud-takeover -> stale-recovery transition and asserts the four
AC-6 safety properties *continuously* over the recorded timeline. The remaining tests pin
each underlying primitive (epoch authority, fence token, idempotent dedupe, edge buffer) so
a regression points at the exact broken guarantee.

Determinism: everything runs on an injected :class:`LogicalClock` and content-derived nudge
ids — no wall clock, no ``random`` — so this test cannot flake.
"""

from __future__ import annotations

from cadence.spikes.s0_3 import (
    BrainNode,
    ChaosSimulator,
    Device,
    FencedDispatcher,
    LeaseError,
    LeaseStore,
    LogicalClock,
    Nudge,
    make_nudge_id,
)

# --------------------------------------------------------------------------------------
# Headline chaos test
# --------------------------------------------------------------------------------------


def test_chaos_failover_holds_all_properties() -> None:
    report = ChaosSimulator(lease_ttl=10).run()

    # Property 1: exactly-one active leader at all times (safety: never two).
    assert report.max_active_leaders == 1, "split-brain: two active leaders observed"
    for snap in report.timeline:
        assert len(snap.active_leaders) <= 1, f"two leaders at {snap.label}: {snap.active_leaders}"

    # Property 1 (liveness): once a term is established the run always has a leader, except
    # the deliberate outage window where the old lease has lapsed and cloud has not yet won.
    outage_labels = {"degraded:selfhost-down-lease-lapsed"}
    for snap in report.timeline:
        if snap.label in outage_labels:
            assert snap.active_leaders == [], f"expected leaderless gap at {snap.label}"
        else:
            assert len(snap.active_leaders) == 1, f"no leader at {snap.label}"

    # Property 2: the fenced stale writer's write was rejected, and it was deposed on renew.
    assert report.fenced_writes == 1, "stale self-host write should have been fenced exactly once"
    assert report.stale_renew_rejected, "stale self-host should be refused when it renews"

    # Property 3: 0 double-nudges across the whole transition.
    assert report.double_nudges == 0, "idempotent dedupe failed: a nudge was delivered twice"

    # Property 4: 0 lost buffered events; the edge buffer fully drained.
    assert report.lost_events == 0, "an edge-buffered event was lost across failover"
    assert report.final_buffer_depth == 0, "device edge buffer did not drain after recovery"

    # Sanity: every captured event was delivered exactly once (3 unique nudges).
    assert report.timeline[-1].delivered_ids == {
        make_nudge_id(ev) for ev in ("e1", "e2", "e3")
    }

    # The fence epoch only ever moves up (never accepts a regression).
    epochs = [s.fence_epoch for s in report.timeline]
    assert epochs == sorted(epochs), "fence epoch regressed"


def test_chaos_new_leader_epoch_strictly_greater() -> None:
    report = ChaosSimulator(lease_ttl=10).run()
    # Cloud's term must carry a strictly higher epoch than the self-host's original term.
    assert report.timeline[0].store_epoch == 1
    assert report.timeline[-1].store_epoch == 2


# --------------------------------------------------------------------------------------
# Primitive: independent lease/epoch authority
# --------------------------------------------------------------------------------------


def test_lease_grants_single_holder_and_bumps_epoch() -> None:
    clock = LogicalClock()
    store = LeaseStore(ttl=5)

    g1 = store.try_acquire("a", clock.now())
    assert g1 is not None and g1.epoch == 1
    # A second node cannot acquire while the lease is live.
    assert store.try_acquire("b", clock.now()) is None
    assert store.live_holder(clock.now()) == "a"


def test_lease_lapses_after_ttl_and_next_term_bumps_epoch() -> None:
    clock = LogicalClock()
    store = LeaseStore(ttl=5)
    store.try_acquire("a", clock.now())  # epoch 1

    clock.advance(5)  # lease expires (now >= expires_at)
    assert store.live_holder(clock.now()) is None
    g2 = store.try_acquire("b", clock.now())
    assert g2 is not None and g2.epoch == 2, "new term must mint a higher epoch"


def test_stale_leader_renew_is_rejected() -> None:
    clock = LogicalClock()
    store = LeaseStore(ttl=5)
    store.try_acquire("a", clock.now())  # epoch 1 held by a
    clock.advance(5)
    store.try_acquire("b", clock.now())  # epoch 2 held by b

    # 'a' comes back and tries to renew its dead epoch -> rejected.
    try:
        store.renew("a", 1, clock.now())
    except LeaseError:
        pass
    else:  # pragma: no cover - defensive
        raise AssertionError("stale renew must raise LeaseError")


def test_owner_renew_keeps_same_epoch() -> None:
    clock = LogicalClock()
    store = LeaseStore(ttl=5)
    store.try_acquire("a", clock.now())  # epoch 1
    clock.advance(2)
    g = store.renew("a", 1, clock.now())
    assert g.epoch == 1, "renewing the owned term must not bump the epoch"


# --------------------------------------------------------------------------------------
# Primitive: fence token
# --------------------------------------------------------------------------------------


def test_fence_rejects_lower_epoch_after_higher_accepted() -> None:
    d = FencedDispatcher()
    hi = Nudge(make_nudge_id("x"), "x", epoch=2, emitted_by="cloud")
    lo = Nudge(make_nudge_id("y"), "y", epoch=1, emitted_by="self-host")

    assert d.dispatch(hi).accepted
    res = d.dispatch(lo)
    assert not res.accepted and res.reason == "fenced"
    assert len(d.fenced) == 1


def test_fence_advances_and_never_regresses() -> None:
    d = FencedDispatcher()
    d.dispatch(Nudge(make_nudge_id("a"), "a", epoch=1, emitted_by="self-host"))
    assert d.max_epoch == 1
    d.dispatch(Nudge(make_nudge_id("b"), "b", epoch=3, emitted_by="cloud"))
    assert d.max_epoch == 3
    # A subsequent epoch-2 write is fenced; max_epoch stays 3.
    d.dispatch(Nudge(make_nudge_id("c"), "c", epoch=2, emitted_by="zombie"))
    assert d.max_epoch == 3


# --------------------------------------------------------------------------------------
# Primitive: idempotent nudge id + dedupe
# --------------------------------------------------------------------------------------


def test_nudge_id_is_content_derived_and_leader_independent() -> None:
    # Same logical event -> same id regardless of which leader emits it.
    assert make_nudge_id("evt-42") == make_nudge_id("evt-42")
    assert make_nudge_id("evt-42") != make_nudge_id("evt-43")


def test_duplicate_same_epoch_nudge_delivered_once() -> None:
    d = FencedDispatcher()
    n1 = Nudge(make_nudge_id("e3"), "e3", epoch=2, emitted_by="cloud")
    n2 = Nudge(make_nudge_id("e3"), "e3", epoch=2, emitted_by="self-host-recovered")

    assert d.dispatch(n1).accepted
    res = d.dispatch(n2)
    assert not res.accepted and res.reason == "duplicate"
    assert d.deduped == 1, "the dedupe guard should have fired on the duplicate"
    assert d.double_delivery_count() == 0, "dedupe must prevent an actual double delivery"
    assert d.delivered_ids == {make_nudge_id("e3")}


# --------------------------------------------------------------------------------------
# Primitive: device edge buffer
# --------------------------------------------------------------------------------------


def test_edge_buffer_retries_fenced_events_against_real_leader() -> None:
    """A fenced drain must leave the event buffered so no event is lost."""
    d = FencedDispatcher()
    d.max_epoch = 2  # a higher term already exists (cloud is leader)
    device = Device("phone")
    device.capture("e9")

    stale = BrainNode("self-host", grant=_grant("self-host", epoch=1))
    device.drain_to(stale, d)  # stale leader -> fenced -> event stays buffered
    assert device.buffer == ["e9"], "fenced event must remain buffered for retry"

    cloud = BrainNode("cloud", grant=_grant("cloud", epoch=2))
    device.drain_to(cloud, d)  # real leader -> delivered -> buffer drains
    assert device.buffer == []
    assert make_nudge_id("e9") in d.delivered_ids


def test_non_leader_node_emits_nothing() -> None:
    d = FencedDispatcher()
    node = BrainNode("self-host")  # no grant -> does not believe it leads
    assert node.emit_nudge("e1", d) is None


def _grant(node_id: str, epoch: int):
    from cadence.spikes.s0_3 import LeaseGrant

    return LeaseGrant(node_id=node_id, epoch=epoch, expires_at=10_000)
