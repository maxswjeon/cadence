using Cadence.WindowsAgent.Compliance;
using Cadence.WindowsAgent.Models;

namespace Cadence.WindowsAgent.Collectors;

/// <summary>
/// Detects that a known online-meeting application is in the foreground, by process name and
/// (where useful) window-title pattern. Subscribes to <see cref="ActiveWindowCollector"/>'s
/// on-change stream — detection is a side effect of the same foreground-window signal
/// ActiveWindowCollector already reads, not a separate polling loop.
///
/// SCOPE (important, per the consensus plan's Decision D / Phase 5 gating): this class ONLY
/// detects that a meeting app is foreground and emits a "device.meeting_detected" signal
/// (see Mapping/EventMapper.cs). It does NOT — and must NOT — capture meeting audio, screen
/// video of the meeting, or join/control the meeting app. Recording is a Phase-5,
/// S0.5-compliance-gated capability (co-presence D2 countdown / explicit online-meeting
/// control, consensus plan Decision D) that requires participant-context confirmation and a
/// visible/audible recording-state indicator neither of which exist in this Phase-1 skeleton.
/// <see cref="BeginMeetingCaptureAsync"/> exists only to make that boundary explicit in code,
/// not as a partially-built feature.
///
/// Process/window-title patterns below are this skeleton's best current knowledge of each
/// app's real executable/window naming and are NOT verified against a live install (no .NET
/// SDK / no meeting apps installed in this environment — see ../README.md). Zoom and classic
/// Teams window titles in particular are known to vary by version; TODO(device): validate
/// against real installs and prefer a documented API/window-class signal over title
/// substring-matching where one exists (e.g. Teams exposes a window class name that is more
/// stable than its title).
/// </summary>
public sealed class MeetingDetector : IDisposable
{
    private static readonly IReadOnlyDictionary<string, string> KnownMeetingProcesses = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase)
    {
        ["Zoom"] = "zoom", // desktop client's main process is conventionally named Zoom.exe
        ["Teams"] = "teams", // classic Teams: Teams.exe
        ["ms-teams"] = "teams", // new Teams (2023 rearchitecture): ms-teams.exe
    };

    // Google Meet has no desktop client — it only ever runs as a browser tab, so it cannot be
    // distinguished from "any other tab" by process name alone. A browser process (chrome,
    // msedge, firefox) whose foreground window title contains a Meet-shaped hint is this
    // skeleton's best-effort signal; TODO(device): this is a real, disclosed limitation, not a
    // bug to silently paper over — a reliable Meet signal likely needs a browser extension or
    // UI-Automation-level tab-title read (see ActiveWindowCollector's UI Automation extension
    // point), not window-title substring matching on the whole browser window.
    private static readonly (string ProcessName, string TitleHint)[] BrowserMeetingHints =
    {
        ("chrome", "Meet"),
        ("msedge", "Meet"),
        ("firefox", "Meet"),
    };

    private readonly ActiveWindowCollector _activeWindowCollector;
    private readonly bool _ownsActiveWindowCollector;

    // The single check BeginMeetingCaptureAsync consults. Defaults to the fail-closed
    // DeniedComplianceGate (see Compliance/IComplianceGate.cs): the authoritative S0.5 decision
    // lives in the brain (cadence.spikes.s0_5.ComplianceGate); this local gate is provisioned by
    // that decision and stays DENIED until it is. Detection below never consults it — only
    // capture does.
    private readonly IComplianceGate _complianceGate;

    public event EventHandler<MeetingDetection>? MeetingDetected;

    public MeetingDetector(
        ActiveWindowCollector? activeWindowCollector = null,
        IComplianceGate? complianceGate = null)
    {
        _ownsActiveWindowCollector = activeWindowCollector is null;
        _activeWindowCollector = activeWindowCollector ?? new ActiveWindowCollector();
        _complianceGate = complianceGate ?? new DeniedComplianceGate();
        _activeWindowCollector.WindowChanged += OnWindowChanged;
    }

    public void Start()
    {
        if (_ownsActiveWindowCollector)
        {
            _activeWindowCollector.Start();
        }
    }

    private void OnWindowChanged(object? sender, WindowInfo info)
    {
        if (info.ProcessName is not { } processName)
        {
            return;
        }

        foreach (var (knownProcess, appName) in KnownMeetingProcesses)
        {
            if (processName.Contains(knownProcess, StringComparison.OrdinalIgnoreCase))
            {
                Raise(appName, info);
                return;
            }
        }

        foreach (var (browserProcess, titleHint) in BrowserMeetingHints)
        {
            if (processName.Contains(browserProcess, StringComparison.OrdinalIgnoreCase)
                && info.Title is { } title
                && title.Contains(titleHint, StringComparison.OrdinalIgnoreCase))
            {
                Raise("google_meet", info);
                return;
            }
        }
    }

    private void Raise(string appName, WindowInfo info)
        => MeetingDetected?.Invoke(this, new MeetingDetection(appName, info.Title, info.CapturedAt));

    /// <summary>
    /// GATED — capture stays refused, and even a permitted path stays unbuilt. This method
    /// consults <see cref="IComplianceGate"/> (the single S0.5 check, see
    /// Compliance/IComplianceGate.cs) and does nothing else: the gate defaults to
    /// <see cref="DeniedComplianceGate"/>, so it refuses cleanly with the gate's reason. Meeting
    /// audio/screen capture requires the S0.5 compliance controls (participant-context confirm,
    /// visible/audible recording-state indicator, one-tap stop, per-trigger audit log,
    /// non-participant abort/purge) that do not exist yet anywhere in this codebase — see
    /// .omc/plans/cadence-consensus-plan.md Decision D / Phase 5 / AC-5.
    ///
    /// Honest posture: there is NO recording code here. If the gate is (hypothetically)
    /// provisioned open, this still throws <see cref="NotImplementedException"/> rather than
    /// recording, so no silent capture path exists even when permitted. Actual audio/screen
    /// capture is the device-specific, post-sign-off step and is intentionally unbuilt.
    /// </summary>
    public Task BeginMeetingCaptureAsync(MeetingDetection detection, CancellationToken cancellationToken = default)
    {
        var decision = _complianceGate.CapturePermitted();
        if (!decision.Permitted)
        {
            // Refuse cleanly with the gate's explainable reason. This is the expected path:
            // the default gate is fail-closed (DENIED).
            throw new NotSupportedException($"GATED: {decision.Reason}");
        }

        // Permitted only reaches here if a provisioned gate opened — but capture is still not
        // built. Fail explicitly instead of recording so there is no silent capture path even
        // when the gate permits it. Real audio/screen capture is the device-specific step that
        // lands only after the S0.5 controls (and operator sign-off) are in place.
        throw new NotImplementedException(
            "GATE OPEN but capture is unbuilt: meeting audio/screen recording is the "
            + "device-specific, post-S0.5-sign-off step and has not been implemented — refusing "
            + "rather than silently recording (Decision D / Phase 5 / AC-5).");
    }

    public void Dispose()
    {
        _activeWindowCollector.WindowChanged -= OnWindowChanged;
        if (_ownsActiveWindowCollector)
        {
            _activeWindowCollector.Dispose();
        }
    }
}

public sealed record MeetingDetection(string AppName, string? WindowTitle, DateTimeOffset DetectedAt);
