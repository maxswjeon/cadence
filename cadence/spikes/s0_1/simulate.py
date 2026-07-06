"""Simulated/mock probe run — the device-free part of S0.1.

A real probe drives a physical accessibility driver against a real device/app and
watches a real control account. Here :class:`SimulatedAccessibilityDriver` stands in
for that driver so the state machine, the forbidden-action assertion layer, and the
control-observation plumbing in :mod:`cadence.spikes.s0_1.protocol` can be exercised
and tested without hardware. It can be told to misbehave (``leak_forbidden_action`` /
``leak_read_mutation``) to prove the safety net actually catches a bad scrape rather
than only ever exercising the happy path.

:func:`run_simulated_probe` orchestrates a full probe attempt for one app: start the
probe, drive N scrape actions through the assertion layer, feed in control
observations, and land on the resulting :class:`~cadence.spikes.s0_1.protocol.ProbeState`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from cadence.spikes.s0_1.protocol import (
    ControlAccountConfig,
    ControlObservation,
    ForbiddenActionError,
    MessengerApp,
    MessengerProbe,
    ProbeState,
)

#: A benign, non-mutating accessibility action every app supports (reading the
#: already-rendered idle-room text without opening/touching the conversation).
SAFE_SCRAPE_ACTION = "read_idle_room_snapshot"


@dataclass
class SimulatedAccessibilityDriver:
    """A fake accessibility driver for one (app, control-account-config) pair.

    ``leak_forbidden_action`` / ``leak_read_mutation`` simulate a scrape-logic bug: the
    driver behaves normally except it also performs a forbidden action, or the
    recipient's unread indicator flips as an unintended side effect. These are the two
    failure modes the real protocol must catch on-device.
    """

    config: ControlAccountConfig
    leak_forbidden_action: str | None = None
    leak_read_mutation: bool = False
    actions_performed: list[str] = field(default_factory=list)

    def scrape_idle_room(self, probe: MessengerProbe) -> ControlObservation:
        """Perform one simulated scrape pass, asserting every action as it happens."""
        probe.assert_action_allowed(SAFE_SCRAPE_ACTION)
        self.actions_performed.append(SAFE_SCRAPE_ACTION)
        if self.leak_forbidden_action is not None:
            # Raises + trips the probe inside assert_action_allowed; propagate to caller.
            probe.assert_action_allowed(self.leak_forbidden_action)
            self.actions_performed.append(self.leak_forbidden_action)  # pragma: no cover
        return ControlObservation(
            recipient_unread_before=True,
            recipient_unread_after=False if self.leak_read_mutation else True,
            outbound_read_events_observed=0,
            unread_negative_control=True,
        )


@dataclass(frozen=True)
class SimulatedProbeResult:
    app: MessengerApp
    final_state: ProbeState
    actions_performed: tuple[str, ...]
    trip_reason: str | None


def run_simulated_probe(
    config: ControlAccountConfig,
    *,
    rounds: int = 3,
    leak_forbidden_action: str | None = None,
    leak_read_mutation: bool = False,
) -> SimulatedProbeResult:
    """Run a full simulated probe attempt for one app and return where it landed.

    A clean run (no leaks injected) passes ``rounds`` observation rounds and reaches
    ``SCRAPING_ENABLED``. Injecting either leak kind trips the probe to
    ``DISABLED_SAFETY_TRIP`` instead — this is the auto-disable-on-failure behavior
    this spike is meant to prove, without needing a device.
    """
    probe = MessengerProbe(config.app)
    probe.start_probe(config)
    driver = SimulatedAccessibilityDriver(
        config,
        leak_forbidden_action=leak_forbidden_action,
        leak_read_mutation=leak_read_mutation,
    )

    trip_reason: str | None = None
    for _ in range(rounds):
        if probe.state is ProbeState.DISABLED_SAFETY_TRIP:
            break
        try:
            observation = driver.scrape_idle_room(probe)
        except ForbiddenActionError as exc:
            trip_reason = str(exc)
            break
        probe.record_observation(observation)

    if probe.state is ProbeState.PROBING:
        probe.pass_probe()
    elif probe.state is ProbeState.DISABLED_SAFETY_TRIP and trip_reason is None:
        trip_reason = probe.history[-1].reason

    return SimulatedProbeResult(
        app=config.app,
        final_state=probe.state,
        actions_performed=tuple(driver.actions_performed),
        trip_reason=trip_reason,
    )


__all__ = [
    "SAFE_SCRAPE_ACTION",
    "SimulatedAccessibilityDriver",
    "SimulatedProbeResult",
    "run_simulated_probe",
]
