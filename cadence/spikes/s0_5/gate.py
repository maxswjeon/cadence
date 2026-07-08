"""Application-level S0.5 capture gate — the single check capture code must consult.

Every S0.5 control in this package (the recording gate, audit log, purge hook, presence
gate) enforces one slice of the compliance posture. :class:`ComplianceGate` is the one
place capture code asks "am I allowed to capture at all right now?" — and the answer is
**CLOSED by default**. It opens only when BOTH:

* the operator has recorded their self-review sign-off (Decision #3) by setting
  ``CADENCE_S0_5_CONFIRMED`` / ``settings.s0_5_confirmed`` to ``True``, and
* every required control is actually wired: a recording gate, an audit log, a purge hook
  **backed by a real NAS delete** (not an in-memory buffer), and a presence gate.

Missing either condition, :meth:`capture_permitted` returns a *specific* reason so the
refusal is explainable. Nothing here is or substitutes for a lawyer's sign-off.
"""

from __future__ import annotations

from dataclasses import dataclass

from cadence.config import Settings, get_settings
from cadence.spikes.s0_5.audit import TriggerAuditLog
from cadence.spikes.s0_5.presence_gate import BLEPresenceGate
from cadence.spikes.s0_5.purge import PurgeHook
from cadence.spikes.s0_5.recording_gate import RecordingGate


@dataclass(frozen=True)
class GateDecision:
    """The gate's answer, with a human-readable reason (always populated)."""

    permitted: bool
    reason: str


class ComplianceGate:
    """CLOSED-by-default gate deciding whether capture may proceed under S0.5.

    The controls are wired at construction; ``None`` means "not deployed". Capture code
    calls :meth:`capture_permitted` and must honor a closed decision — this is the single
    consult point, not a per-control check scattered across the pipeline.
    """

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        recording_gate: RecordingGate | None = None,
        audit: TriggerAuditLog | None = None,
        purge_hook: PurgeHook | None = None,
        presence_gate: BLEPresenceGate | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._recording_gate = recording_gate
        self._audit = audit
        self._purge_hook = purge_hook
        self._presence_gate = presence_gate

    def capture_permitted(self) -> GateDecision:
        """Whether capture may proceed right now. CLOSED unless confirmed + fully wired."""
        if not self._settings.s0_5_confirmed:
            return GateDecision(
                permitted=False,
                reason=(
                    "capture refused: s0_5_confirmed is False — the operator has not "
                    "confirmed the S0.5 controls are deployed and self-reviewed "
                    "(set CADENCE_S0_5_CONFIRMED once that sign-off is done)"
                ),
            )
        missing: list[str] = []
        if self._recording_gate is None:
            missing.append("recording_gate")
        if self._audit is None:
            missing.append("audit_log")
        if self._purge_hook is None:
            missing.append("purge_hook")
        elif not self._purge_hook.backed_by_real_delete:
            missing.append("purge_hook(real_delete)")
        if self._presence_gate is None:
            missing.append("presence_gate")
        if missing:
            return GateDecision(
                permitted=False,
                reason=(
                    "capture refused: required S0.5 controls not wired: "
                    + ", ".join(missing)
                ),
            )
        return GateDecision(
            permitted=True,
            reason=(
                "capture permitted: s0_5_confirmed and all S0.5 controls wired "
                "(recording gate, audit log, real-delete purge hook, presence gate)"
            ),
        )


__all__ = ["GateDecision", "ComplianceGate"]
