using System.Security.Cryptography;
using System.Text;
using Cadence.WindowsAgent.Models;

namespace Cadence.WindowsAgent.Mapping;

/// <summary>
/// Maps this agent's raw signals (WindowInfo, app-usage samples, meeting detections, screen
/// context) onto <see cref="EventEnvelope"/>, the contract-shaped wire type (see
/// ../Models/EventEnvelope.cs for the full raw-boundary/schema-reconciliation notes).
///
/// Every method here is real, runnable mapping logic — the parts that are NOT implemented are
/// the collectors that call in (they own the actual OS/device capture, marked TODO(device) in
/// ../Collectors/*.cs). This class only ever sees already-collected, already-policy-decided
/// values; it must never widen what a collector chose to omit (e.g. it will not "helpfully"
/// pull a raw window title into Structured if a collector left it out for privacy reasons).
///
/// Naming convention (reconciled against `contract/event-envelope.schema.json` + the Android
/// sibling's `EventMapper.kt`, both landed after this file was first written): `source` is
/// per-signal (`"windows_active_window"`, not one generic `"windows_agent"` bucket) — this
/// matches the schema's own worked examples, which literally name `"android_notification"` and
/// `"windows_active_window"` as the intended per-signal shape. `kind` is platform-agnostic
/// (`"device.active_window"`, not `"windows.active_window"`) — the schema's examples use
/// `"device.notification"`/`"device.app_usage"`, and Android's mapper independently settled on
/// the same `"device.*"` shape; platform distinction lives in `source`, not `kind`.
/// </summary>
public static class EventMapper
{
    public const string SourceActiveWindow = "windows_active_window";
    public const string SourceAppUsage = "windows_app_usage";
    public const string SourceMeetingDetected = "windows_meeting_detected";
    public const string SourceScreenContext = "windows_screen_context";

    public static EventEnvelope FromActiveWindow(
        WindowInfo window, string deviceId, string accountRef)
    {
        var eventId = $"active_window:{deviceId}:{window.CapturedAt:O}";
        return new EventEnvelope
        {
            EventId = eventId,
            Source = SourceActiveWindow,
            AccountRef = accountRef,
            AcquisitionTier = AcquisitionTier.DeviceOsApi,
            Kind = "device.active_window",
            OccurredAt = window.CapturedAt,
            DeviceId = deviceId,
            DedupeId = ComputeDedupeId(SourceActiveWindow, accountRef, eventId),
            // Non-verbatim by construction: a process name is an app identity, not document
            // content. The window Title is deliberately NOT copied here — see the raw-boundary
            // note on EventEnvelope; a future pass may add a redacted/truncated title once a
            // policy for what's safe to keep is agreed (S0.5-adjacent, not yet decided for
            // this specific field).
            Structured = new Dictionary<string, object?>
            {
                ["process_name"] = window.ProcessName,
                ["process_id"] = window.ProcessId,
            },
        };
    }

    public static EventEnvelope FromAppUsageSample(
        string processName, TimeSpan foregroundDuration, DateTimeOffset windowStart, string deviceId, string accountRef)
    {
        var eventId = $"app_usage:{deviceId}:{processName}:{windowStart:O}";
        return new EventEnvelope
        {
            EventId = eventId,
            Source = SourceAppUsage,
            AccountRef = accountRef,
            AcquisitionTier = AcquisitionTier.DeviceOsApi,
            Kind = "device.app_usage",
            OccurredAt = windowStart,
            DeviceId = deviceId,
            DedupeId = ComputeDedupeId(SourceAppUsage, accountRef, eventId),
            Structured = new Dictionary<string, object?>
            {
                ["process_name"] = processName,
                ["foreground_seconds"] = foregroundDuration.TotalSeconds,
                ["window_start"] = windowStart,
            },
        };
    }

    /// <summary>
    /// Maps a MeetingDetector hit. This event only ever records that a known meeting app
    /// (Zoom/Teams/Meet) was foreground — it never carries meeting content, and this mapper has
    /// no path to attach audio/transcript data (recording capture is gated; see
    /// ../Collectors/MeetingDetector.cs).
    /// </summary>
    public static EventEnvelope FromMeetingDetected(
        string appName, string? windowTitleHint, DateTimeOffset detectedAt, string deviceId, string accountRef)
    {
        var eventId = $"meeting_detected:{deviceId}:{appName}:{detectedAt:O}";
        return new EventEnvelope
        {
            EventId = eventId,
            Source = SourceMeetingDetected,
            AccountRef = accountRef,
            AcquisitionTier = AcquisitionTier.DeviceOsApi,
            Kind = "device.meeting_detected",
            OccurredAt = detectedAt,
            DeviceId = deviceId,
            DedupeId = ComputeDedupeId(SourceMeetingDetected, accountRef, eventId),
            Summary = windowTitleHint is null ? null : "meeting app in foreground",
            Structured = new Dictionary<string, object?>
            {
                ["app"] = appName,
            },
        };
    }

    /// <summary>
    /// Maps an on-change/active-window screen-context capture. The frame/OCR bytes are gated
    /// (see ../Collectors/ScreenContextCollector.cs) — by the time a real implementation calls
    /// this, capture must already have written the raw frame to the NAS and produced
    /// <paramref name="rawEvidenceRef"/> + <paramref name="payloadHash"/>; this mapper never
    /// accepts raw bytes directly (there is no byte[]/Stream parameter on purpose).
    /// </summary>
    public static EventEnvelope FromScreenContext(
        string processName, string rawEvidenceRef, string payloadHash, string? nonVerbatimSummary,
        DateTimeOffset capturedAt, string deviceId, string accountRef)
    {
        var eventId = $"screen_context:{deviceId}:{capturedAt:O}";
        return new EventEnvelope
        {
            EventId = eventId,
            Source = SourceScreenContext,
            AccountRef = accountRef,
            AcquisitionTier = AcquisitionTier.DeviceOsApi,
            Kind = "device.screen_context",
            OccurredAt = capturedAt,
            DeviceId = deviceId,
            DedupeId = ComputeDedupeId(SourceScreenContext, accountRef, eventId),
            RawEvidenceRef = rawEvidenceRef,
            PayloadHash = payloadHash,
            Summary = nonVerbatimSummary,
            Structured = new Dictionary<string, object?>
            {
                ["process_name"] = processName,
                ["trigger"] = "active_window_change",
            },
        };
    }

    /// <summary>
    /// Mirrors the brain's <c>Event.with_dedupe_id()</c> derivation exactly: sha256("source|account_ref|event_id")
    /// as a lowercase hex digest (contract/protocol.md §4 — the brain's own fallback formula
    /// when a device doesn't set dedupe_id itself). See cadence/adapters/base.py — kept
    /// identical so the brain's own dedupe path and this agent's pre-computed id agree if the
    /// core (W2) does not override it with its own basis.
    /// </summary>
    private static string ComputeDedupeId(string source, string accountRef, string eventId)
    {
        var basis = $"{source}|{accountRef}|{eventId}";
        var bytes = SHA256.HashData(Encoding.UTF8.GetBytes(basis));
        // Convert.ToHexStringLower is .NET 9+; this project targets net8.0-windows, so lower-case explicitly.
        return Convert.ToHexString(bytes).ToLowerInvariant();
    }
}
