using System.Runtime.InteropServices;

namespace Cadence.WindowsAgent.Interop;

/// <summary>
/// Raw Win32 declarations backing ActiveWindowCollector. Signatures verified against MS Learn
/// (winuser.h, User32.dll):
///  - GetForegroundWindow: https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-getforegroundwindow
///  - GetWindowTextW / GetWindowTextLengthW: https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-getwindowtextw
///  - GetWindowThreadProcessId: https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-getwindowthreadprocessid
///  - SetWinEventHook / UnhookWinEvent / WinEventProc: https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-setwineventhook ,
///    https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-unhookwinevent ,
///    https://learn.microsoft.com/en-us/windows/win32/api/winuser/nc-winuser-wineventproc
///
/// Mixed P/Invoke style, deliberately: functions with only blittable/simple parameters use the
/// .NET 7+ source-generated LibraryImportAttribute (recommended for new code —
/// https://learn.microsoft.com/en-us/dotnet/standard/native-interop/pinvoke-source-generation).
/// Two functions keep classic DllImportAttribute instead, because the source generator does not
/// support their parameter shapes (confirmed against
/// https://github.com/dotnet/runtime/blob/main/docs/design/libraries/LibraryImportGenerator/Compatibility.md):
/// GetWindowTextW takes a StringBuilder out-buffer (StringBuilder marshalling is unsupported by
/// LibraryImport in any form); SetWinEventHook/UnhookWinEvent take/return a managed delegate
/// (WinEventProc) and an HWINEVENTHOOK handle that the source generator does not marshal as
/// cleanly as classic DllImport.
/// </summary>
internal static partial class NativeMethods
{
    // -- EVENT_SYSTEM_FOREGROUND range (winuser.h) --------------------------------------- //
    // https://learn.microsoft.com/en-us/windows/win32/winauto/event-constants
    internal const uint EVENT_SYSTEM_FOREGROUND = 0x0003;

    // dwFlags for SetWinEventHook.
    internal const uint WINEVENT_OUTOFCONTEXT = 0x0000;
    internal const uint WINEVENT_SKIPOWNPROCESS = 0x0002;

    /// <summary>
    /// Matches the WINEVENTPROC callback shape (winuser.h nc-winuser-wineventproc):
    /// void CALLBACK WinEventProc(HWINEVENTHOOK hWinEventHook, DWORD event, HWND hwnd,
    ///                            LONG idObject, LONG idChild, DWORD idEventThread, DWORD dwmsEventTime);
    /// TODO(device): per MS Learn's remarks on SetWinEventHook, pin this delegate with
    /// GCHandle (or keep a static field reference) so the GC never relocates/collects it while
    /// the hook is live — a dangling callback pointer is a real crash risk, not just a lint nit.
    /// </summary>
    internal delegate void WinEventProc(
        nint hWinEventHook,
        uint eventType,
        nint hwnd,
        int idObject,
        int idChild,
        uint idEventThread,
        uint dwmsEventTime);

    /// <summary>HWND GetForegroundWindow(void);</summary>
    [LibraryImport("user32.dll")]
    internal static partial nint GetForegroundWindow();

    /// <summary>int GetWindowTextLengthW(HWND hWnd);</summary>
    [LibraryImport("user32.dll", EntryPoint = "GetWindowTextLengthW", SetLastError = true)]
    internal static partial int GetWindowTextLengthW(nint hWnd);

    /// <summary>
    /// int GetWindowTextW(HWND hWnd, LPWSTR lpString, int nMaxCount);
    /// Kept on classic DllImportAttribute, not LibraryImport: the source generator does not
    /// support marshalling System.Text.StringBuilder parameters at all (confirmed against
    /// https://github.com/dotnet/runtime/blob/main/docs/design/libraries/LibraryImportGenerator/Compatibility.md,
    /// "consumers should retain uses of DllImportAttribute" for StringBuilder).
    /// </summary>
    [DllImport("user32.dll", EntryPoint = "GetWindowTextW", CharSet = CharSet.Unicode, SetLastError = true)]
    internal static extern int GetWindowTextW(nint hWnd, System.Text.StringBuilder lpString, int nMaxCount);

    /// <summary>DWORD GetWindowThreadProcessId(HWND hWnd, LPDWORD lpdwProcessId);</summary>
    [LibraryImport("user32.dll", SetLastError = true)]
    internal static partial uint GetWindowThreadProcessId(nint hWnd, out uint lpdwProcessId);

    /// <summary>
    /// HWINEVENTHOOK SetWinEventHook(DWORD eventMin, DWORD eventMax, HMODULE hmodWinEventProc,
    ///                                WINEVENTPROC pfnWinEventProc, DWORD idProcess, DWORD idThread, DWORD dwFlags);
    /// Returns NULL (nint.Zero) on failure.
    /// </summary>
    [DllImport("user32.dll", SetLastError = true)]
    internal static extern nint SetWinEventHook(
        uint eventMin,
        uint eventMax,
        nint hmodWinEventProc,
        WinEventProc pfnWinEventProc,
        uint idProcess,
        uint idThread,
        uint dwFlags);

    /// <summary>BOOL UnhookWinEvent(HWINEVENTHOOK hWinEventHook);</summary>
    [DllImport("user32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    internal static extern bool UnhookWinEvent(nint hWinEventHook);
}
