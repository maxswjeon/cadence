namespace Cadence.WindowsAgent.Models;

/// <summary>
/// Snapshot of the current foreground window, as read via Win32
/// (GetForegroundWindow/GetWindowText/GetWindowThreadProcessId) and optionally enriched via
/// UI Automation (AutomationElement). See ../Collectors/ActiveWindowCollector.cs.
/// </summary>
public sealed record WindowInfo
{
    /// <summary>Raw HWND value (IntPtr) at capture time — NOT stable across window lifetime, do not persist as an id.</summary>
    public required nint WindowHandle { get; init; }

    /// <summary>Window title via GetWindowText. May contain sensitive content (e.g. a document
    /// name, a chat participant) — collectors must not pass this verbatim into
    /// EventEnvelope.Structured; see EventMapper's raw-boundary handling.</summary>
    public string? Title { get; init; }

    /// <summary>Owning process id, from GetWindowThreadProcessId's out parameter.</summary>
    public uint ProcessId { get; init; }

    /// <summary>Process (executable) name, e.g. "Teams.exe", resolved via Process.GetProcessById.
    /// TODO(device): resolving MainModule.FileName can throw Win32Exception for
    /// elevated/protected processes (access denied) — collectors must catch and fall back to
    /// ProcessName only.</summary>
    public string? ProcessName { get; init; }

    public DateTimeOffset CapturedAt { get; init; } = DateTimeOffset.UtcNow;
}
