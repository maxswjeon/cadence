"""Participant-context confirm + cancellable-countdown state machine (Decision D2).

"A recording trigger never silently starts capture of a possibly-non-party
conversation; a cancellable countdown + participant-context confirmation is required"
(consensus plan, Decision D). This module is the state machine that enforces that:

* A trigger always starts a **cancellable countdown** — never recording immediately.
* If the countdown expires **without** the participant context having been confirmed
  as "all parties present", the session holds at ``AWAITING_CONFIRM`` rather than
  auto-starting the recording — fail-closed, matching the S0.1 "escalate, never
  silently fall back to a mutating/risky path" posture used elsewhere in this repo.
* Confirming ``NON_PARTICIPANT_PRESENT`` at **any** point (armed, counting down,
  already recording) immediately aborts and invokes the :class:`~cadence.spikes.s0_5.
  purge.PurgeHook` — the non-participant abort/purge control.
* Every transition is written to the :class:`~cadence.spikes.s0_5.audit.TriggerAuditLog`
  and reflected on the :class:`~cadence.spikes.s0_5.indicator.RecordingStateIndicator`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from cadence.spikes.s0_5.audit import TriggerAuditLog
from cadence.spikes.s0_5.indicator import IndicatorState, RecordingStateIndicator
from cadence.spikes.s0_5.purge import PurgeHook


class TriggerState(StrEnum):
    IDLE = "idle"
    COUNTDOWN = "countdown"
    AWAITING_CONFIRM = "awaiting_confirm"
    RECORDING = "recording"
    CANCELLED = "cancelled"
    ABORTED_NON_PARTICIPANT = "aborted_non_participant"
    STOPPED = "stopped"


class ParticipantContext(StrEnum):
    """Whether everyone captured is a conversation the owner is a party to."""

    UNKNOWN = "unknown"
    ALL_PARTICIPANTS = "all_participants"
    NON_PARTICIPANT_PRESENT = "non_participant_present"


_TERMINAL_STATES = frozenset(
    {TriggerState.CANCELLED, TriggerState.ABORTED_NON_PARTICIPANT, TriggerState.STOPPED}
)

_INDICATOR_FOR_STATE = {
    TriggerState.IDLE: IndicatorState.OFF,
    TriggerState.COUNTDOWN: IndicatorState.COUNTDOWN,
    TriggerState.AWAITING_CONFIRM: IndicatorState.COUNTDOWN,
    TriggerState.RECORDING: IndicatorState.RECORDING,
    TriggerState.CANCELLED: IndicatorState.OFF,
    TriggerState.ABORTED_NON_PARTICIPANT: IndicatorState.STOPPED,
    TriggerState.STOPPED: IndicatorState.STOPPED,
}


@dataclass
class RecordingSession:
    session_id: str
    source: str
    state: TriggerState = TriggerState.IDLE
    participant_context: ParticipantContext = ParticipantContext.UNKNOWN
    countdown_remaining: int = 0


class RecordingGate:
    """Drives one or more :class:`RecordingSession`\\ s through the D2 lifecycle."""

    def __init__(
        self,
        *,
        countdown_ticks: int,
        audit: TriggerAuditLog,
        indicator: RecordingStateIndicator,
        purge_hook: PurgeHook,
    ) -> None:
        self._default_countdown = countdown_ticks
        self._audit = audit
        self._indicator = indicator
        self._purge_hook = purge_hook
        self._sessions: dict[str, RecordingSession] = {}

    def _show(self, session: RecordingSession) -> None:
        self._indicator.show(_INDICATOR_FOR_STATE[session.state])

    def trigger(self, session_id: str, source: str) -> RecordingSession:
        """Arm a fresh session: always starts a cancellable countdown, never recording."""
        session = RecordingSession(
            session_id=session_id, source=source, state=TriggerState.COUNTDOWN,
            countdown_remaining=self._default_countdown,
        )
        self._sessions[session_id] = session
        self._audit.record(
            session_id, source, "armed", detail=f"countdown={session.countdown_remaining}"
        )
        self._show(session)
        return session

    def cancel(self, session_id: str) -> RecordingSession:
        """User-cancels during the countdown (or the awaiting-confirm hold)."""
        session = self._require(session_id)
        if session.state not in (TriggerState.COUNTDOWN, TriggerState.AWAITING_CONFIRM):
            raise ValueError(f"cannot cancel a session in state {session.state!r}")
        session.state = TriggerState.CANCELLED
        self._audit.record(session_id, session.source, "cancelled")
        self._show(session)
        return session

    def tick(self, session_id: str) -> RecordingSession:
        """Advance the countdown by one tick.

        On reaching zero: if the participant context is already confirmed as
        ``ALL_PARTICIPANTS``, start recording; otherwise hold at
        ``AWAITING_CONFIRM`` (fail-closed — never auto-record an unconfirmed context).
        """
        session = self._require(session_id)
        if session.state is not TriggerState.COUNTDOWN:
            raise ValueError(f"no countdown in progress (state={session.state!r})")
        session.countdown_remaining = max(0, session.countdown_remaining - 1)
        if session.countdown_remaining > 0:
            return session
        if session.participant_context is ParticipantContext.ALL_PARTICIPANTS:
            session.state = TriggerState.RECORDING
            detail = "countdown elapsed, all-participants"
            self._audit.record(session_id, session.source, "confirmed", detail=detail)
        else:
            session.state = TriggerState.AWAITING_CONFIRM
            detail = "countdown elapsed, unconfirmed"
            self._audit.record(session_id, session.source, "awaiting_confirm", detail=detail)
        self._show(session)
        return session

    def confirm_participant_context(
        self, session_id: str, context: ParticipantContext
    ) -> RecordingSession:
        """Set/update the participant-context confirmation.

        A ``NON_PARTICIPANT_PRESENT`` confirmation aborts + purges immediately,
        regardless of current state (short of an already-terminal one). An
        ``ALL_PARTICIPANTS`` confirmation while counting down or awaiting confirm lets
        recording (re)start on the next tick / immediately if already past countdown.
        """
        session = self._require(session_id)
        if session.state in _TERMINAL_STATES:
            raise ValueError(f"session {session_id!r} is already terminal ({session.state!r})")
        session.participant_context = context
        if context is ParticipantContext.NON_PARTICIPANT_PRESENT:
            session.state = TriggerState.ABORTED_NON_PARTICIPANT
            self._purge_hook.purge(session_id, reason="non-participant confirmed present")
            self._audit.record(session_id, session.source, "aborted_non_participant")
            self._show(session)
            return session
        was_awaiting = session.state is TriggerState.AWAITING_CONFIRM
        if context is ParticipantContext.ALL_PARTICIPANTS and was_awaiting:
            session.state = TriggerState.RECORDING
            self._audit.record(session_id, session.source, "confirmed", detail="explicit confirm")
            self._show(session)
        return session

    def stop(self, session_id: str) -> RecordingSession:
        """One-tap stop — valid only while actively recording."""
        session = self._require(session_id)
        if session.state is not TriggerState.RECORDING:
            raise ValueError(f"cannot stop a session in state {session.state!r}")
        session.state = TriggerState.STOPPED
        self._audit.record(session_id, session.source, "stopped")
        self._show(session)
        return session

    def _require(self, session_id: str) -> RecordingSession:
        try:
            return self._sessions[session_id]
        except KeyError:
            raise KeyError(f"no session {session_id!r}") from None


__all__ = ["TriggerState", "ParticipantContext", "RecordingSession", "RecordingGate"]
