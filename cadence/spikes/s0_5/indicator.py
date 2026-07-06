"""Recording-state-indicator interface (Decision D mandatory control).

A recording trigger must never be invisible to the people around it: a visible/audible
indicator reflects the current state, and a one-tap stop must always be reachable while
recording. This module defines the interface; :class:`InMemoryRecordingIndicator` is
the test/spike double (a real implementation would drive a phone notification/LED/audio
cue, or the office-Pi's on-device indicator from Decision I).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import StrEnum


class IndicatorState(StrEnum):
    """What the indicator shows. Mirrors the recording-gate lifecycle 1:1."""

    OFF = "off"
    COUNTDOWN = "countdown"
    RECORDING = "recording"
    STOPPED = "stopped"


class RecordingStateIndicator(ABC):
    """Visible/audible recording-state indicator with a one-tap stop signal."""

    @abstractmethod
    def show(self, state: IndicatorState) -> None:
        """Update the visible/audible indicator to ``state``."""

    @abstractmethod
    def current(self) -> IndicatorState:
        """The indicator's current displayed state."""

    @abstractmethod
    def request_stop(self) -> None:
        """Record a one-tap-stop request from the user (polled via :meth:`stop_requested`)."""

    @abstractmethod
    def stop_requested(self) -> bool:
        """Whether a one-tap stop has been requested since the last :meth:`show`."""


class InMemoryRecordingIndicator(RecordingStateIndicator):
    """Test/spike double: just remembers state + stop-request flag."""

    def __init__(self) -> None:
        self._state = IndicatorState.OFF
        self._stop_requested = False
        self.history: list[IndicatorState] = [self._state]

    def show(self, state: IndicatorState) -> None:
        self._state = state
        self.history.append(state)
        # A fresh state transition clears a stale stop request from a prior session.
        if state in (IndicatorState.OFF, IndicatorState.STOPPED):
            self._stop_requested = False

    def current(self) -> IndicatorState:
        return self._state

    def request_stop(self) -> None:
        self._stop_requested = True

    def stop_requested(self) -> bool:
        return self._stop_requested


__all__ = ["IndicatorState", "RecordingStateIndicator", "InMemoryRecordingIndicator"]
