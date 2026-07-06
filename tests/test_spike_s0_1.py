"""Tests for S0.1 — the read-state safety probe protocol (device-free).

These exercise the state machine, the forbidden-action assertion layer, and the
simulated probe run — the parts of S0.1 that are runnable without a real device. The
on-device empirical verdict is gated (see ``.omc/research/spikes/s0_1.md``); nothing
here fabricates one.
"""

from __future__ import annotations

import pytest

from cadence.spikes.s0_1.protocol import (
    ControlAccountConfig,
    ControlObservation,
    ForbiddenActionError,
    MessengerApp,
    MessengerProbe,
    ProbeState,
)
from cadence.spikes.s0_1.simulate import run_simulated_probe


def _config(app: MessengerApp = MessengerApp.KAKAOTALK) -> ControlAccountConfig:
    return ControlAccountConfig(
        app=app,
        device_os_version="android14",
        app_version="10.2.0",
        sender_control_account="control-sender",
        recipient_control_account="control-recipient",
    )


# --- state machine ---------------------------------------------------------------- #


def test_probe_starts_observe_only() -> None:
    probe = MessengerProbe(MessengerApp.KAKAOTALK)
    assert probe.state is ProbeState.OBSERVE_ONLY


def test_probe_passes_with_clean_observations() -> None:
    probe = MessengerProbe(MessengerApp.KAKAOTALK)
    probe.start_probe(_config())
    assert probe.state is ProbeState.PROBING
    clean = ControlObservation(
        recipient_unread_before=True,
        recipient_unread_after=True,
        outbound_read_events_observed=0,
        unread_negative_control=True,
    )
    probe.record_observation(clean)
    assert probe.state is ProbeState.PROBING
    probe.pass_probe()
    assert probe.state is ProbeState.SCRAPING_ENABLED


def test_probe_trips_on_unread_indicator_flip() -> None:
    probe = MessengerProbe(MessengerApp.LINE)
    probe.start_probe(_config(MessengerApp.LINE))
    mutated = ControlObservation(
        recipient_unread_before=True,
        recipient_unread_after=False,  # negative-control message got marked read
        outbound_read_events_observed=0,
        unread_negative_control=True,
    )
    probe.record_observation(mutated)
    assert probe.state is ProbeState.DISABLED_SAFETY_TRIP


def test_probe_trips_on_outbound_read_event() -> None:
    probe = MessengerProbe(MessengerApp.JANDI)
    probe.start_probe(_config(MessengerApp.JANDI))
    leaked = ControlObservation(
        recipient_unread_before=True,
        recipient_unread_after=True,
        outbound_read_events_observed=1,
        unread_negative_control=True,
    )
    probe.record_observation(leaked)
    assert probe.state is ProbeState.DISABLED_SAFETY_TRIP


def test_forbidden_action_raises_and_trips() -> None:
    probe = MessengerProbe(MessengerApp.KAKAOTALK)
    probe.start_probe(_config())
    with pytest.raises(ForbiddenActionError):
        probe.assert_action_allowed("mark_as_read")
    assert probe.state is ProbeState.DISABLED_SAFETY_TRIP


def test_failed_probe_lands_on_observe_only_not_a_mutating_state() -> None:
    """Escalate, never silently fall back to a mutating path (Principle 1)."""
    probe = MessengerProbe(MessengerApp.KAKAOWORK)
    probe.start_probe(_config(MessengerApp.KAKAOWORK))
    probe.fail_probe("inconclusive control-account run")
    assert probe.state is ProbeState.OBSERVE_ONLY


def test_disabled_safety_trip_is_terminal_until_explicit_reprobe() -> None:
    probe = MessengerProbe(MessengerApp.KAKAOTALK)
    probe.start_probe(_config())
    with pytest.raises(ForbiddenActionError):
        probe.assert_action_allowed("mark_as_read")
    assert probe.state is ProbeState.DISABLED_SAFETY_TRIP
    # Cannot start a fresh probe directly from a safety trip without going through
    # OBSERVE_ONLY first — there is no silent auto-recovery path.
    with pytest.raises(ValueError):
        probe.start_probe(_config())


def test_reprobe_cadence_triggers_on_app_version_change() -> None:
    probe = MessengerProbe(MessengerApp.NAVER, reprobe_interval_days=14)
    probe.start_probe(_config(MessengerApp.NAVER))
    probe.pass_probe()
    assert probe.due_for_reprobe(probe.last_probed_at, "10.2.0") is False
    assert probe.due_for_reprobe(probe.last_probed_at, "10.3.0") is True  # app updated


def test_force_reprobe_suspends_scraping_until_repassed() -> None:
    probe = MessengerProbe(MessengerApp.LINE_WORKS)
    probe.start_probe(_config(MessengerApp.LINE_WORKS))
    probe.pass_probe()
    probe.force_reprobe("app updated 10.2.0 -> 10.3.0")
    assert probe.state is ProbeState.PROBING  # not SCRAPING_ENABLED during re-probe


# --- per-app isolation (Claude M3) ------------------------------------------------- #


def test_each_app_gated_independently() -> None:
    """Passing KakaoTalk's probe must not affect LINE's (or any other app's) state."""
    kakao = MessengerProbe(MessengerApp.KAKAOTALK)
    kakao.start_probe(_config(MessengerApp.KAKAOTALK))
    kakao.pass_probe()

    line = MessengerProbe(MessengerApp.LINE)
    assert line.state is ProbeState.OBSERVE_ONLY
    assert kakao.state is ProbeState.SCRAPING_ENABLED


# --- simulated probe run (device-free harness) ------------------------------------- #


def test_simulated_probe_passes_cleanly() -> None:
    result = run_simulated_probe(_config())
    assert result.final_state is ProbeState.SCRAPING_ENABLED
    assert result.trip_reason is None
    assert len(result.actions_performed) == 3  # rounds default


def test_simulated_probe_catches_a_forbidden_action_leak() -> None:
    result = run_simulated_probe(_config(), leak_forbidden_action="mark_as_read")
    assert result.final_state is ProbeState.DISABLED_SAFETY_TRIP
    assert result.trip_reason is not None
    assert "mark_as_read" in result.trip_reason


def test_simulated_probe_catches_a_read_mutation_leak() -> None:
    result = run_simulated_probe(_config(MessengerApp.LINE), leak_read_mutation=True)
    assert result.final_state is ProbeState.DISABLED_SAFETY_TRIP
    assert result.trip_reason is not None


@pytest.mark.parametrize("app", list(MessengerApp))
def test_simulated_probe_runs_for_every_gated_app(app: MessengerApp) -> None:
    """Sanity: the protocol/forbidden-action table covers all five apps + KakaoTalk."""
    result = run_simulated_probe(_config(app))
    assert result.final_state is ProbeState.SCRAPING_ENABLED
