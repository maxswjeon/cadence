namespace Cadence.WindowsAgent.Compliance;

/// <summary>
/// Fail-closed <see cref="IComplianceGate"/>: <see cref="CapturePermitted"/> always returns a
/// DENIED decision. This is the correct, intentional default for the current phase — not a
/// placeholder to be flipped to "allow" locally.
///
/// The authoritative S0.5 decision lives in the brain
/// (<c>cadence.spikes.s0_5.ComplianceGate</c>, which is itself CLOSED by default and opens only
/// on the operator's S0.5 sign-off). A deployed agent's gate is meant to be *provisioned* from
/// that decision; this implementation is what ships until such provisioning exists, so the
/// agent stays fail-closed (DENIED) rather than assuming permission it was never granted.
/// There is deliberately no constructor parameter or setting that turns this gate "open" — a
/// real open path is a later, sign-off-gated change, and even then the actual capture body
/// stays unbuilt (see <see cref="Collectors.MeetingDetector.BeginMeetingCaptureAsync"/>).
/// </summary>
public sealed class DeniedComplianceGate : IComplianceGate
{
    public CaptureDecision CapturePermitted()
        => new(
            Permitted: false,
            Reason:
                "capture DENIED (fail-closed): this agent's local compliance gate has not been "
                + "provisioned with an S0.5 authorization from the brain "
                + "(cadence.spikes.s0_5.ComplianceGate). Meeting capture stays refused until the "
                + "operator has confirmed the S0.5 controls and that decision reaches this agent.");
}
