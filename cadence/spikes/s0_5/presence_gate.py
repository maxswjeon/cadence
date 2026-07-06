"""BLE owner-presence power-gate interface (Decision I, default-OFF).

Per the corrected legal posture (``.omc/research/korea-office-mic-legal-risk.md``,
Decision I): the office-Pi mic must be **hardware-unpowered/muted unless the owner's
phone is confirmed present**, and the feature as a whole ships **default-OFF** — a user
must explicitly opt in before the presence gate does anything at all. This module is
the interface only; there is no real BLE/Pi hardware here (see ``AGENTS.md``'s
"Do NOT build here" list — the office-Pi capture pipeline is out of scope for this
milestone). What *is* implemented and tested: the default-OFF + presence-derived power
gating logic, and the single-subject (owner-only) voiceprint-enrollment invariant that
keeps this out of PIPA §23 sensitive-data territory.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


class PresenceSource(ABC):
    """Reports whether the owner's phone is currently detected present (BLE)."""

    @abstractmethod
    def owner_present(self) -> bool: ...


@dataclass
class StaticPresenceSource(PresenceSource):
    """Test/spike double: presence is just a flag the harness flips."""

    present: bool = False

    def owner_present(self) -> bool:
        return self.present


class OwnerOnlyVoiceprintViolation(RuntimeError):
    """Raised if anything but the single enrolled owner id is ever enrolled/matched."""


class OwnerVoiceprintEnrollment:
    """Single-subject voiceprint enrollment (Decision I: owner-only, never third-party).

    Enrolling a second, different subject id is refused outright — this is the code-level
    enforcement of "never build/match third-party voiceprints", which is what keeps this
    node out of the separate PIPA §23 sensitive-biometric-data violation.
    """

    def __init__(self) -> None:
        self._owner_id: str | None = None

    @property
    def is_enrolled(self) -> bool:
        return self._owner_id is not None

    def enroll(self, owner_id: str) -> None:
        if self._owner_id is not None and self._owner_id != owner_id:
            raise OwnerOnlyVoiceprintViolation(
                f"voiceprint already enrolled for owner {self._owner_id!r}; "
                f"refusing to enroll a second subject {owner_id!r} "
                "(single-subject, owner-only invariant)"
            )
        self._owner_id = owner_id

    def matches_owner(self, speaker_id: str) -> bool:
        """True only for the enrolled owner id; every other speaker is 'unknown'."""
        return self._owner_id is not None and speaker_id == self._owner_id


class BLEPresenceGate:
    """Default-OFF power gate: the mic is only ever considered "on" when both

    (a) the feature has been explicitly ``enable()``\\ d by the user, and
    (b) the owner's phone is currently BLE-present.

    Absent either condition the mic is treated as hardware-unpowered — there is no
    ambient-listening state reachable from this gate.
    """

    def __init__(self, presence_source: PresenceSource) -> None:
        self._presence_source = presence_source
        self._enabled = False  # default-OFF; must be explicitly turned on

    @property
    def enabled(self) -> bool:
        return self._enabled

    def enable(self) -> None:
        self._enabled = True

    def disable(self) -> None:
        self._enabled = False

    def is_mic_powered(self) -> bool:
        """Whether the mic should be powered right now (never true while disabled)."""
        if not self._enabled:
            return False
        return self._presence_source.owner_present()


__all__ = [
    "PresenceSource",
    "StaticPresenceSource",
    "OwnerOnlyVoiceprintViolation",
    "OwnerVoiceprintEnrollment",
    "BLEPresenceGate",
]
