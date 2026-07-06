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

    public event EventHandler<MeetingDetection>? MeetingDetected;

    public MeetingDetector(ActiveWindowCollector? activeWindowCollector = null)
    {
        _ownsActiveWindowCollector = activeWindowCollector is null;
        _activeWindowCollector = activeWindowCollector ?? new ActiveWindowCollector();
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
    /// GATED — deliberately not implemented. Meeting audio/screen capture requires the
    /// S0.5 compliance controls (participant-context confirm, visible/audible recording-state
    /// indicator, one-tap stop, per-trigger audit log, non-participant abort/purge) that do not
    /// exist yet anywhere in this codebase. Do not fill this in without those controls landing
    /// first — see .omc/plans/cadence-consensus-plan.md Decision D / Phase 5 / AC-5.
    /// </summary>
    public Task BeginMeetingCaptureAsync(MeetingDetection detection, CancellationToken cancellationToken = default)
        => throw new NotSupportedException(
            "GATED: meeting audio/screen capture requires the S0.5 compliance-controls gate (Decision D, Phase 5) — not implemented.");

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
