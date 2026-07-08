using System.Runtime.InteropServices;

namespace Cadence.WindowsAgent.Interop;

/// <summary>
/// Helpers for reading the Rust core's thread-local last-error across the C ABI, honoring its
/// ownership contract: <see cref="CoreInterop.CadenceCoreLastError"/> returns a caller-owned heap
/// <c>char*</c> (<c>{"code":i32,"message":"..."}</c> JSON) that MUST be released with
/// <see cref="CoreInterop.CadenceStringFree"/>, and reading it clears the error.
/// </summary>
internal static class CoreErrors
{
    /// <summary>
    /// Read and clear the current thread's last-error as its raw JSON string, or a placeholder when
    /// none is set. Always frees the core-owned string.
    /// </summary>
    internal static string ReadLastError()
    {
        nint ptr = CoreInterop.CadenceCoreLastError();
        if (ptr == nint.Zero)
        {
            return "(no last error recorded)";
        }
        try
        {
            return Marshal.PtrToStringUTF8(ptr) ?? "(unreadable last error)";
        }
        finally
        {
            CoreInterop.CadenceStringFree(ptr);
        }
    }
}
