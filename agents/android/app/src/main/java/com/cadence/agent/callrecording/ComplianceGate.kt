package com.cadence.agent.callrecording

/**
 * The single on-device check capture code must consult before doing anything with call
 * recordings. Mirrors the brain's authoritative S0.5 gate
 * (`cadence.spikes.s0_5.ComplianceGate.capture_permitted()` → `GateDecision(permitted,
 * reason)`), which is **CLOSED by default** and opens only when the operator has recorded
 * their S0.5 self-review sign-off AND every required control is wired.
 *
 * On device this is *not* where the S0.5 decision is made — the authoritative decision
 * lives in the brain. This local gate is merely provisioned by the brain's decision (the
 * device is told "you may capture" only after the operator confirms S0.5 server-side) and
 * defaults to DENIED / fail-closed until then. There is intentionally no local "open"
 * path: nothing in this skeleton flips the gate to permitted, because doing so is a
 * post-sign-off, brain-driven step, not a device-side toggle.
 */
interface ComplianceGate {

    /** Whether call-recording capture may proceed right now, with an explainable reason. */
    fun capturePermitted(): Decision

    /** The gate's answer. `reason` is always populated so a refusal is explainable. */
    data class Decision(val permitted: Boolean, val reason: String)
}

/**
 * The default gate: DENIED, always. Fail-closed until the brain provisions a permitted
 * decision from a confirmed S0.5 sign-off (Decision D / consensus-plan.md §S0.5). This is
 * the only [ComplianceGate] this skeleton wires — see the class doc on [ComplianceGate]
 * for why there is deliberately no "open" implementation here.
 */
object DeniedComplianceGate : ComplianceGate {
    override fun capturePermitted(): ComplianceGate.Decision =
        ComplianceGate.Decision(
            permitted = false,
            reason = (
                "capture refused: no confirmed S0.5 sign-off provisioned to this device — " +
                    "the authoritative decision lives in the brain " +
                    "(cadence.spikes.s0_5.ComplianceGate), and this local gate defaults to " +
                    "DENIED (fail-closed) until the operator confirms S0.5"
            ),
        )
}
