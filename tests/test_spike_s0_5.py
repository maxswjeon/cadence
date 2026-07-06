"""Tests for S0.5 — compliance technical controls (Decisions D + E + H + I).

Covers every control built for this spike: the D2 recording gate (countdown +
participant confirm + non-participant abort/purge), the recording-state indicator, the
per-trigger audit log, the retention/destruction TTL job, the BLE owner-presence
power-gate (default-OFF) + single-subject voiceprint invariant, and the CODEF
credential-vault/revoke/backoff hooks (built on the existing NAS-only vault). See
``.omc/research/spikes/s0_5.md`` for the controls checklist; legal sign-off itself is
the user's (no counsel here), per user decision #3.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from cadence.adapters.vault import FileCredentialVault
from cadence.config import Settings
from cadence.spikes.s0_5.audit import TriggerAuditLog
from cadence.spikes.s0_5.codef_vault import CODEFBackoff, CODEFCredentialManager, CODEFLockedOut
from cadence.spikes.s0_5.indicator import IndicatorState, InMemoryRecordingIndicator
from cadence.spikes.s0_5.presence_gate import (
    BLEPresenceGate,
    OwnerOnlyVoiceprintViolation,
    OwnerVoiceprintEnrollment,
    StaticPresenceSource,
)
from cadence.spikes.s0_5.purge import InMemoryBufferPurgeHook, RetainedRecording, RetentionPurgeJob
from cadence.spikes.s0_5.recording_gate import ParticipantContext, RecordingGate, TriggerState

# --- recording gate (Decision D2) -------------------------------------------------- #


_GateFixture = tuple[
    RecordingGate, TriggerAuditLog, InMemoryRecordingIndicator, InMemoryBufferPurgeHook
]


def _gate() -> _GateFixture:
    audit = TriggerAuditLog()
    indicator = InMemoryRecordingIndicator()
    purge_hook = InMemoryBufferPurgeHook(audit, source="co_presence")
    gate = RecordingGate(countdown_ticks=3, audit=audit, indicator=indicator, purge_hook=purge_hook)
    return gate, audit, indicator, purge_hook


def test_trigger_always_starts_a_countdown_never_records_immediately() -> None:
    gate, _, indicator, _ = _gate()
    session = gate.trigger("s1", "co_presence")
    assert session.state is TriggerState.COUNTDOWN
    assert indicator.current() is IndicatorState.COUNTDOWN


def test_cancel_during_countdown_never_records() -> None:
    gate, audit, indicator, _ = _gate()
    gate.trigger("s1", "co_presence")
    session = gate.cancel("s1")
    assert session.state is TriggerState.CANCELLED
    assert indicator.current() is IndicatorState.OFF
    assert [e.event for e in audit.for_session("s1")] == ["armed", "cancelled"]


def test_countdown_expiry_without_confirm_holds_fail_closed() -> None:
    """Never auto-record an unconfirmed participant context."""
    gate, _, indicator, _ = _gate()
    gate.trigger("s1", "co_presence")
    for _ in range(3):
        session = gate.tick("s1")
    assert session.state is TriggerState.AWAITING_CONFIRM
    assert indicator.current() is IndicatorState.COUNTDOWN  # still not "recording"


def test_explicit_confirm_after_countdown_starts_recording() -> None:
    gate, audit, indicator, _ = _gate()
    gate.trigger("s1", "co_presence")
    for _ in range(3):
        gate.tick("s1")
    session = gate.confirm_participant_context("s1", ParticipantContext.ALL_PARTICIPANTS)
    assert session.state is TriggerState.RECORDING
    assert indicator.current() is IndicatorState.RECORDING
    assert "confirmed" in [e.event for e in audit.for_session("s1")]


def test_confirm_before_countdown_expires_lets_recording_start_on_last_tick() -> None:
    gate, _, _, _ = _gate()
    gate.trigger("s1", "co_presence")
    gate.tick("s1")
    gate.tick("s1")
    gate.confirm_participant_context("s1", ParticipantContext.ALL_PARTICIPANTS)
    session = gate.tick("s1")  # final tick, context already confirmed
    assert session.state is TriggerState.RECORDING


def test_non_participant_confirmation_aborts_and_purges_at_any_state() -> None:
    gate, audit, indicator, purge_hook = _gate()
    gate.trigger("s1", "co_presence")
    purge_hook.write("s1", b"partial-audio-bytes")
    session = gate.confirm_participant_context("s1", ParticipantContext.NON_PARTICIPANT_PRESENT)
    assert session.state is TriggerState.ABORTED_NON_PARTICIPANT
    assert "s1" not in purge_hook.buffers  # purge hook actually destroyed the buffer
    assert indicator.current() is IndicatorState.STOPPED
    events = [e.event for e in audit.for_session("s1")]
    assert "aborted_non_participant" in events
    assert "purged" in events


def test_non_participant_confirmation_aborts_even_while_recording() -> None:
    gate, _, _, purge_hook = _gate()
    gate.trigger("s1", "co_presence")
    for _ in range(3):
        gate.tick("s1")
    gate.confirm_participant_context("s1", ParticipantContext.ALL_PARTICIPANTS)
    purge_hook.write("s1", b"more-bytes")
    session = gate.confirm_participant_context("s1", ParticipantContext.NON_PARTICIPANT_PRESENT)
    assert session.state is TriggerState.ABORTED_NON_PARTICIPANT
    assert "s1" not in purge_hook.buffers


def test_one_tap_stop_only_valid_while_recording() -> None:
    gate, audit, indicator, _ = _gate()
    gate.trigger("s1", "co_presence")
    with pytest.raises(ValueError):
        gate.stop("s1")  # not recording yet
    for _ in range(3):
        gate.tick("s1")
    gate.confirm_participant_context("s1", ParticipantContext.ALL_PARTICIPANTS)
    session = gate.stop("s1")
    assert session.state is TriggerState.STOPPED
    assert indicator.current() is IndicatorState.STOPPED
    assert "stopped" in [e.event for e in audit.for_session("s1")]


def test_cannot_act_on_a_terminal_session() -> None:
    gate, _, _, _ = _gate()
    gate.trigger("s1", "co_presence")
    gate.cancel("s1")
    with pytest.raises(ValueError):
        gate.confirm_participant_context("s1", ParticipantContext.ALL_PARTICIPANTS)


# --- retention/destruction TTL job -------------------------------------------------- #


def test_retention_job_purges_only_expired_recordings() -> None:
    audit = TriggerAuditLog()
    job = RetentionPurgeJob(audit=audit)
    now = datetime.now(tz=UTC)
    job.register(RetainedRecording("old", "meeting", now - timedelta(days=100), timedelta(days=90)))
    job.register(RetainedRecording("fresh", "meeting", now, timedelta(days=90)))

    purged = job.run(now)

    assert purged == ["old"]
    assert job.is_purged("old")
    assert not job.is_purged("fresh")
    assert [e.event for e in audit.for_session("old")] == ["purged"]


def test_retention_job_invokes_on_purge_callback_and_is_idempotent() -> None:
    audit = TriggerAuditLog()
    job = RetentionPurgeJob(audit=audit)
    now = datetime.now(tz=UTC)
    rec = RetainedRecording("old", "phone_call", now - timedelta(days=10), timedelta(days=1))
    job.register(rec)
    destroyed: list[str] = []

    first_pass = job.run(now, on_purge=destroyed.append)
    second_pass = job.run(now, on_purge=destroyed.append)

    assert first_pass == ["old"]
    assert second_pass == []  # already purged; not purged/audited twice
    assert destroyed == ["old"]


# --- BLE owner-presence power gate (Decision I) ------------------------------------ #


def test_ble_gate_is_default_off_even_when_owner_present() -> None:
    presence = StaticPresenceSource(present=True)
    gate = BLEPresenceGate(presence)
    assert gate.enabled is False
    assert gate.is_mic_powered() is False


def test_ble_gate_powers_mic_only_when_enabled_and_owner_present() -> None:
    presence = StaticPresenceSource(present=False)
    gate = BLEPresenceGate(presence)
    gate.enable()
    assert gate.is_mic_powered() is False  # owner away -> unpowered even when enabled
    presence.present = True
    assert gate.is_mic_powered() is True
    gate.disable()
    assert gate.is_mic_powered() is False


def test_owner_voiceprint_enrollment_is_single_subject() -> None:
    enrollment = OwnerVoiceprintEnrollment()
    enrollment.enroll("owner-1")
    assert enrollment.matches_owner("owner-1") is True
    assert enrollment.matches_owner("colleague-2") is False
    with pytest.raises(OwnerOnlyVoiceprintViolation):
        enrollment.enroll("colleague-2")  # never a second (third-party) subject
    enrollment.enroll("owner-1")  # re-enrolling the same owner is fine (idempotent)


# --- CODEF credential-vault/revoke/backoff hooks (built on the NAS-only vault) ----- #


def test_codef_manager_register_get_revoke_via_nas_only_vault(settings: Settings) -> None:
    vault = FileCredentialVault(settings)
    manager = CODEFCredentialManager(vault)
    manager.register("account-1", "connected-id-abc")
    assert manager.connected_id("account-1") == "connected-id-abc"
    assert "account-1" in manager.accounts()
    manager.revoke("account-1")
    assert "account-1" not in manager.accounts()
    with pytest.raises(KeyError):
        manager.connected_id("account-1")


def test_codef_backoff_trips_lockout_after_repeated_failures() -> None:
    backoff = CODEFBackoff(trip_after=3, base_delay=timedelta(seconds=1))
    now = datetime.now(tz=UTC)
    backoff.before_call("acct", now)  # no failures yet -> fine
    backoff.record_failure("acct", now)
    backoff.record_failure("acct", now)
    assert backoff.is_locked("acct", now) is False
    backoff.record_failure("acct", now)  # 3rd consecutive failure trips the lockout
    assert backoff.is_locked("acct", now) is True
    with pytest.raises(CODEFLockedOut):
        backoff.before_call("acct", now)


def test_codef_backoff_success_clears_the_failure_streak() -> None:
    backoff = CODEFBackoff(trip_after=2, base_delay=timedelta(seconds=1))
    now = datetime.now(tz=UTC)
    backoff.record_failure("acct", now)
    backoff.record_success("acct")
    assert backoff.failure_count("acct") == 0
    assert backoff.is_locked("acct", now) is False


def test_codef_backoff_lockout_expires_after_the_delay() -> None:
    backoff = CODEFBackoff(trip_after=1, base_delay=timedelta(seconds=30))
    now = datetime.now(tz=UTC)
    backoff.record_failure("acct", now)
    assert backoff.is_locked("acct", now) is True
    later = now + timedelta(seconds=31)
    assert backoff.is_locked("acct", later) is False
