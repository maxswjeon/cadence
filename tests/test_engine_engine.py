"""AttentionEngine periodic tick — full loop wiring (engine Component 4)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from _engine_util import battery, make_deadline, make_task, spread_activity

from cadence.engine.attention import AttentionState
from cadence.engine.engine import AttentionEngine
from cadence.engine.governor import NudgeGovernor
from cadence.obs.alarms import get_alarm_sink
from cadence.stores.models import Fact, Nudge

NOW = datetime(2026, 7, 7, 15, 0, tzinfo=UTC)


def _seed_canonical(store) -> None:
    refactor = make_task(
        store, "Refactor legacy billing module", priority=1, source_event_ids=["gh-refactor"]
    )
    report = make_task(
        store, "Ship Q3 investor report", priority=4, source_event_ids=["gh-report"]
    )
    make_deadline(store, refactor.id, NOW + timedelta(days=7))
    make_deadline(store, report.id, NOW + timedelta(days=1))
    spread_activity(store, "billing module refactor", NOW, count=11, step_seconds=120)


def test_tick_fires_misallocation_nudge_live(store) -> None:
    _seed_canonical(store)
    engine = AttentionEngine(store, NudgeGovernor(store, mode="live"))
    tick = engine.evaluate(NOW)

    assert tick.snapshot.state is AttentionState.IMMERSED
    assert len(tick.misallocations) == 1
    assert len(tick.nudges) == 1 and tick.nudges[0].kind == "attention.misallocation"
    with store.session() as session:
        assert session.query(Nudge).filter(Nudge.kind == "attention.misallocation").count() == 1


def test_tick_in_shadow_mode_persists_no_nudge(store) -> None:
    _seed_canonical(store)
    gov = NudgeGovernor(store, mode="shadow")
    tick = AttentionEngine(store, gov).evaluate(NOW)
    assert len(tick.misallocations) == 1
    assert gov.proposals  # proposed, not delivered
    with store.session() as session:
        assert session.query(Nudge).count() == 0


def test_repeated_ticks_do_not_double_fire(store) -> None:
    _seed_canonical(store)
    engine = AttentionEngine(store, NudgeGovernor(store, mode="live"))
    engine.evaluate(NOW)
    engine.evaluate(NOW + timedelta(minutes=1))  # scheduler ticks again shortly after
    with store.session() as session:
        assert session.query(Nudge).filter(Nudge.kind == "attention.misallocation").count() == 1


def test_tick_also_runs_device_care_rule(store) -> None:
    battery(store, 7, NOW, device="phone")
    engine = AttentionEngine(store, NudgeGovernor(store, mode="live"))
    tick = engine.evaluate(NOW)
    kinds = {n.kind for n in tick.nudges}
    assert "device.care" in kinds


def test_tick_uses_constructor_clock_when_now_omitted(store) -> None:
    _seed_canonical(store)
    engine = AttentionEngine(store, NudgeGovernor(store, mode="live"), clock=lambda: NOW)
    tick = engine.evaluate()
    assert tick.now == NOW
    assert len(tick.nudges) == 1


def test_tick_can_persist_attention_and_stays_boundary_clean(store) -> None:
    _seed_canonical(store)
    engine = AttentionEngine(
        store, NudgeGovernor(store, mode="live"), persist_attention=True
    )
    engine.evaluate(NOW)
    with store.session() as session:
        assert session.query(Fact).filter(Fact.kind == "attention.state").count() == 1
    # Whole tick writes only structured/summary rows.
    assert get_alarm_sink().count("raw_to_cloud_violation") == 0


def test_ingest_seam_is_off_by_default(store) -> None:
    _seed_canonical(store)
    engine = AttentionEngine(store, NudgeGovernor(store, mode="live"))
    assert engine.on_ingest(NOW) is None  # seam disabled → no immediate eval
    with store.session() as session:
        assert session.query(Nudge).count() == 0

    engine.evaluate_on_ingest = True
    tick = engine.on_ingest(NOW)
    assert tick is not None and len(tick.nudges) == 1
