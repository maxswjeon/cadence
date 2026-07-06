using System.Text;
using Cadence.WindowsAgent.Interop;
using Cadence.WindowsAgent.Models;

namespace Cadence.WindowsAgent.Collectors;

/// <summary>
/// Watches the Windows foreground window and raises <see cref="WindowChanged"/> on every
/// change (on-change, per the consensus plan's capture invariant — never a polling loop that
/// re-reads on a fixed timer, and never continuous).
///
/// Approach: an out-of-context <c>SetWinEventHook</c> on <c>EVENT_SYSTEM_FOREGROUND</c>
/// (see ../Interop/NativeMethods.cs) delivers a callback exactly when the foreground window
/// changes; each callback re-reads the window's title/process via <c>GetWindowTextW</c> +
/// <c>GetWindowThreadProcessId</c>. This satisfies the plan's "foreground window via UI
/// Automation / Win32 GetForegroundWindow" wording via the Win32 half; an optional UI
/// Automation enrichment path (<see cref="TryEnrichWithFocusedAutomationElement"/>) is left as
/// a documented extension point for callers that need finer detail than a window title (e.g. a
/// browser's active tab, exposed through UI Automation's ControlType/Name properties on the
/// focused element) — see System.Windows.Automation.AutomationElement.FocusedElement:
/// https://learn.microsoft.com/en-us/dotnet/api/system.windows.automation.automationelement.focusedelement
///
/// TODO(device): every P/Invoke call in this file is written against the documented Win32
/// signatures (cited in NativeMethods.cs) but has never been exercised on a real Windows
/// machine or under a real .NET runtime — this box has no .NET SDK (see ../README.md). Treat
/// this class as unverified until it has actually run.
/// </summary>
public sealed class ActiveWindowCollector : IDisposable
{
    private readonly NativeMethods.WinEventProc _callback;
    private nint _hookHandle;

    public event EventHandler<WindowInfo>? WindowChanged;

    public ActiveWindowCollector()
    {
        // Keep a strong reference to the delegate for the collector's lifetime — SetWinEventHook's
        // docs warn the callback must not be garbage-collected/moved while the hook is live.
        _callback = OnForegroundChanged;
    }

    /// <summary>Installs the EVENT_SYSTEM_FOREGROUND hook. Call once from the hosting service's
    /// StartAsync. TODO(device): confirm the calling thread has a message loop — MS Learn notes
    /// "the client thread that calls SetWinEventHook must have a message loop in order to
    /// receive events," which a plain BackgroundService thread does NOT have by default; this
    /// likely needs its own STA thread pumping messages (e.g. via Application.Run or a manual
    /// GetMessage/DispatchMessage loop), not the host's async Task loop.</summary>
    public void Start()
    {
        if (_hookHandle != 0)
        {
            return;
        }

        _hookHandle = NativeMethods.SetWinEventHook(
            NativeMethods.EVENT_SYSTEM_FOREGROUND,
            NativeMethods.EVENT_SYSTEM_FOREGROUND,
            hmodWinEventProc: 0,
            _callback,
            idProcess: 0,
            idThread: 0,
            NativeMethods.WINEVENT_OUTOFCONTEXT | NativeMethods.WINEVENT_SKIPOWNPROCESS);

        if (_hookHandle == 0)
        {
            // TODO(device): SetLastError = true is set on the DllImport; surface
            // Marshal.GetLastWin32Error() here via a real logging path once one exists.
            throw new InvalidOperationException("SetWinEventHook failed to install the foreground-change hook.");
        }
    }

    public void Stop()
    {
        if (_hookHandle == 0)
        {
            return;
        }

        NativeMethods.UnhookWinEvent(_hookHandle);
        _hookHandle = 0;
    }

    private void OnForegroundChanged(
        nint hWinEventHook, uint eventType, nint hwnd, int idObject, int idChild, uint idEventThread, uint dwmsEventTime)
    {
        if (hwnd == 0)
        {
            return;
        }

        var info = ReadWindowInfo(hwnd);
        if (info is not null)
        {
            WindowChanged?.Invoke(this, info);
        }
    }

    /// <summary>Reads title + owning process for a window handle. Also callable directly (e.g.
    /// for an initial snapshot at startup via GetForegroundWindow(), before the first change
    /// event fires).</summary>
    private static WindowInfo? ReadWindowInfo(nint hwnd)
    {
        var length = NativeMethods.GetWindowTextLengthW(hwnd);
        string? title = null;
        if (length > 0)
        {
            var buffer = new StringBuilder(length + 1);
            var copied = NativeMethods.GetWindowTextW(hwnd, buffer, buffer.Capacity);
            if (copied > 0)
            {
                title = buffer.ToString();
            }
        }

        NativeMethods.GetWindowThreadProcessId(hwnd, out var processId);

        string? processName = null;
        try
        {
            using var process = System.Diagnostics.Process.GetProcessById((int)processId);
            processName = process.ProcessName;
        }
        catch (Exception)
        {
            // TODO(device): narrow this to the specific exceptions GetProcessById/MainModule can
            // throw (ArgumentException for an already-exited pid, Win32Exception for
            // access-denied on a protected/elevated process) once this runs against real
            // processes; for now any failure just means "process name unavailable."
        }

        return new WindowInfo
        {
            WindowHandle = hwnd,
            Title = title,
            ProcessId = processId,
            ProcessName = processName,
        };
    }

    /// <summary>
    /// Extension point: UI Automation can resolve richer detail than a window title alone
    /// (e.g. a browser's focused document/tab via the automation tree), via
    /// System.Windows.Automation.AutomationElement.FocusedElement (UIAutomationClient.dll,
    /// enabled by this project's &lt;UseWPF&gt; — see CadenceWindowsAgent.csproj).
    /// TODO(device): not implemented — left as a documented stub. A concrete implementation
    /// must call UI Automation from its own dedicated thread (MS Learn: "If your client
    /// application might try to find elements in its own user interface, you must make all UI
    /// Automation calls on a separate thread") and must respect the same non-verbatim
    /// boundary EventMapper enforces — do not smuggle a document's full text out through here.
    /// </summary>
    private static void TryEnrichWithFocusedAutomationElement(WindowInfo info)
    {
        throw new NotImplementedException(
            "TODO(device): UI Automation enrichment (AutomationElement.FocusedElement) — not implemented in this skeleton.");
    }

    public void Dispose() => Stop();
}
