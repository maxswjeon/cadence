using Cadence.WindowsAgent.Models;

namespace Cadence.WindowsAgent.Collectors;

/// <summary>
/// Triggers a screen-context capture **on foreground/active-window change only** — never on a
/// timer, never continuously, and never while the screen is locked/off. This mirrors the
/// consensus plan's explicit capture invariant (Decision G / §Data Sources: "a frame+OCR only
/// on foreground app/window change or meaningful activity; screen-off = nothing; no always-on
/// screen video") and AC-1's capture-integrity scope.
///
/// The trigger/debounce wiring below (subscribing to ActiveWindowCollector, deciding *when* a
/// capture should fire) is real logic. The actual pixel capture is a GATED stub — see
/// <see cref="CaptureFrameAsync"/> — even though screen capture itself is not S0.5-gated the
/// way audio/co-presence recording is (per the milestone-2 plan, this collector's capture body
/// is called out as a documented stub deliberately, to avoid landing an untested capture path
/// with no NAS-write / retention / OCR pipeline behind it yet).
/// </summary>
public sealed class ScreenContextCollector : IDisposable
{
    private readonly ActiveWindowCollector _activeWindowCollector;
    private readonly bool _ownsActiveWindowCollector;

    public event EventHandler<ScreenContextCapture>? CaptureReady;

    public ScreenContextCollector(ActiveWindowCollector? activeWindowCollector = null)
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

    private async void OnWindowChanged(object? sender, WindowInfo info)
    {
        // TODO(device): guard against capturing while the workstation is locked/screen is off
        // (e.g. via Microsoft.Win32.SystemEvents.SessionSwitch /
        // WTSRegisterSessionNotification) — the plan's "screen-off = nothing" invariant is not
        // yet enforced here; a foreground-change event technically can still fire around a
        // lock transition and must be filtered out before any capture is attempted.
        var capture = await CaptureFrameAsync(info, CancellationToken.None).ConfigureAwait(false);
        if (capture is not null)
        {
            CaptureReady?.Invoke(this, capture);
        }
    }

    /// <summary>
    /// GATED — deliberately not implemented. A real implementation would grab the current
    /// screen contents (e.g. via the Windows.Graphics.Capture WinRT API for a modern
    /// per-window/per-monitor capture session, or the classic GDI
    /// System.Drawing.Graphics.CopyFromScreen for a simple full-screen bitmap), write the raw
    /// frame (and any OCR text extracted from it) to the NAS as raw evidence, and only then
    /// call EventMapper.FromScreenContext with the resulting rawEvidenceRef/payloadHash — never
    /// pass raw bytes into the mapper directly (see Mapping/EventMapper.cs's raw-boundary
    /// note). None of that NAS-write/OCR/retention pipeline exists yet in this repo, so this
    /// stub intentionally returns null rather than a half-real implementation with nowhere to
    /// safely put its output.
    /// </summary>
    private Task<ScreenContextCapture?> CaptureFrameAsync(WindowInfo info, CancellationToken cancellationToken)
        => throw new NotSupportedException(
            "GATED: screen-frame capture is a documented stub in this skeleton pending a NAS-write/OCR/retention pipeline — not implemented.");

    public void Dispose()
    {
        _activeWindowCollector.WindowChanged -= OnWindowChanged;
        if (_ownsActiveWindowCollector)
        {
            _activeWindowCollector.Dispose();
        }
    }
}

/// <summary>Already-persisted capture result (raw bytes live on the NAS, not here) — the shape
/// EventMapper.FromScreenContext expects.</summary>
public sealed record ScreenContextCapture(
    string ProcessName, string RawEvidenceRef, string PayloadHash, string? NonVerbatimSummary, DateTimeOffset CapturedAt);
