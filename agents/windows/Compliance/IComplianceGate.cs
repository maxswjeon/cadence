namespace Cadence.WindowsAgent.Compliance;

/// <summary>
/// The single check any capture code on the Windows agent must consult before recording:
/// "am I allowed to capture at all right now?". This is the C# mirror of the brain's
/// authoritative gate — <c>cadence.spikes.s0_5.ComplianceGate.capture_permitted()</c> — which
/// is CLOSED by default and opens only on the operator's S0.5 self-review sign-off with every
/// required control (recording gate, audit log, real-delete purge hook, presence gate) wired.
///
/// IMPORTANT (authority): the real decision does NOT live here. The brain owns it; a deployed
/// agent is expected to be *provisioned* with the brain's current decision (e.g. a gate whose
/// answer is refreshed from the brain over the agent's mTLS channel). Until that provisioning
/// exists, the shipped implementation is <see cref="DeniedComplianceGate"/> — fail-closed,
/// DENIED by default. There is deliberately no local "open" path in this skeleton: an agent
/// must never self-authorize capture.
/// </summary>
public interface IComplianceGate
{
    /// <summary>
    /// Whether capture may proceed right now, with a human-readable <see cref="CaptureDecision.Reason"/>
    /// that is always populated so a refusal is explainable. Mirrors the brain gate's
    /// <c>GateDecision(permitted, reason)</c> shape.
    /// </summary>
    CaptureDecision CapturePermitted();
}

/// <summary>
/// The gate's answer. Local counterpart of the brain's <c>GateDecision</c> — a
/// <see cref="Reason"/> is always set (even when <see cref="Permitted"/> is <c>true</c>) so the
/// decision is self-explaining in logs and audit trails.
/// </summary>
public sealed record CaptureDecision(bool Permitted, string Reason);
