using System.Runtime.InteropServices;

namespace Cadence.WindowsAgent.Interop;

/// <summary>
/// P/Invoke boundary to <c>cadence-agent-core</c> — the shared Rust crate (agents/core, W2 in
/// the milestone-2 plan) that owns the security-critical device logic ONCE, shared with the
/// Android agent via JNI and this agent via P/Invoke: persistent WAL + crash-safe replay,
/// dedupe-id assignment, bounded-queue backpressure, and the mTLS transport client/retry loop.
/// See .omc/plans/cadence-milestone-2-device-agents.md ("Architecture note").
///
/// This is bound 1:1 against the crate's canonical C ABI, hand-written in
/// <c>agents/core/include/cadence_agent_core.h</c> and exported from <c>agents/core/src/ffi.rs</c>
/// as <c>#[no_mangle] pub extern "C"</c> symbols. Semantics that mirror the header:
/// <list type="bullet">
///   <item><b>Handle model.</b> <see cref="CadenceCoreInit"/> returns an opaque
///   <c>CadenceCore*</c> as an <see cref="nint"/> (0 == NULL == failure; read
///   <see cref="CadenceCoreLastError"/>). Every other call takes that handle;
///   <see cref="CadenceCoreShutdown"/> compacts and frees it and is the ONLY thing that does.</item>
///   <item><b>Error convention.</b> Calls return a <see cref="CadenceStatus"/> and/or a pointer
///   (NULL on failure), plus a thread-local last-error readable via
///   <see cref="CadenceCoreLastError"/>.</item>
///   <item><b>Capture vs drain.</b> <see cref="CadenceCoreCapture"/> is a local WAL append
///   (returns <see cref="CadenceStatus.QueueFull"/> as the backpressure signal, mirroring the
///   brain's 503); the brain-side 202/200/503/422 outcomes live in the DrainReport JSON returned
///   by <see cref="CadenceCoreDrain"/>.</item>
///   <item><b>String ownership.</b> Every <c>char*</c> the core returns (dedupe id, drain report,
///   last error) is heap-allocated and owned by THIS caller — free it with
///   <see cref="CadenceStringFree"/> (NULL-safe). Strings passed in are borrowed.
///   <see cref="CadenceCoreVersion"/> returns a static string that must NOT be freed.</item>
/// </list>
///
/// TODO(device): the matching <c>cadence_agent_core.dll</c> (from
/// <c>cargo build --release</c> of agents/core with <c>crate-type = ["cdylib"]</c>) must be
/// packaged next to this agent's binaries so the loader can resolve <see cref="CoreLibrary"/> at
/// runtime; no cdylib is shipped in this repo skeleton.
///
/// P/Invoke pattern reference: LibraryImportAttribute (.NET 7+ source-generated marshalling,
/// recommended over DllImportAttribute for new interop code) —
/// https://learn.microsoft.com/en-us/dotnet/standard/native-interop/pinvoke-source-generation
/// </summary>
internal static partial class CoreInterop
{
    // The cdylib output name from agents/core/Cargo.toml (`[lib] name` defaults to the package
    // name with '-' → '_'): `cadence_agent_core` → `cadence_agent_core.dll` on Windows.
    private const string CoreLibrary = "cadence_agent_core";

    /// <summary>
    /// <c>CadenceCore* cadence_core_init(const char* config_json)</c> — opens/creates the WAL and
    /// builds the mTLS transport from a UTF-8 JSON config (wal_path, capacity, base_url,
    /// client_identity_pem, ca_pem, optional retry{base_ms,max_ms,max_attempts}). Returns a
    /// non-zero opaque handle on success, or 0 on failure (see <see cref="CadenceCoreLastError"/>).
    /// </summary>
    [LibraryImport(CoreLibrary, EntryPoint = "cadence_core_init", StringMarshalling = StringMarshalling.Utf8)]
    internal static partial nint CadenceCoreInit(string configJson);

    /// <summary>
    /// <c>void cadence_core_shutdown(CadenceCore* handle)</c> — compacts the WAL, then frees the
    /// handle. NULL-safe. This is the only call that frees a handle; do not use it afterward.
    /// </summary>
    [LibraryImport(CoreLibrary, EntryPoint = "cadence_core_shutdown")]
    internal static partial void CadenceCoreShutdown(nint handle);

    /// <summary>
    /// <c>CadenceStatus cadence_core_capture(CadenceCore* handle, const char* envelope_json,
    /// char** out_dedupe_id)</c> — durably appends one serialized EventEnvelope (UTF-8 JSON,
    /// matching contract/event-envelope.schema.json) to the WAL and assigns/returns its
    /// dedupe id.
    /// <para>
    /// On <see cref="CadenceStatus.Ok"/>, <paramref name="outDedupeId"/> receives a heap
    /// <c>char*</c> the caller MUST free with <see cref="CadenceStringFree"/>. On
    /// <see cref="CadenceStatus.QueueFull"/> (backpressure — the bounded WAL is full, mirroring
    /// the brain's 503) the out-param is left untouched and the caller must slow down.
    /// </para>
    /// </summary>
    [LibraryImport(CoreLibrary, EntryPoint = "cadence_core_capture", StringMarshalling = StringMarshalling.Utf8)]
    internal static partial CadenceStatus CadenceCoreCapture(nint handle, string envelopeJson, out nint outDedupeId);

    /// <summary>
    /// <c>uint64_t cadence_core_pending_len(const CadenceCore* handle)</c> — count of un-acked
    /// events buffered in the WAL. Returns 0 on a NULL handle.
    /// </summary>
    [LibraryImport(CoreLibrary, EntryPoint = "cadence_core_pending_len")]
    internal static partial ulong CadenceCorePendingLen(nint handle);

    /// <summary>
    /// <c>int32_t cadence_core_is_full(const CadenceCore* handle)</c> — 1 when the bounded queue
    /// is full (caller must pause capture), 0 otherwise, -1 on a NULL handle.
    /// </summary>
    [LibraryImport(CoreLibrary, EntryPoint = "cadence_core_is_full")]
    internal static partial int CadenceCoreIsFull(nint handle);

    /// <summary>
    /// <c>char* cadence_core_drain(CadenceCore* handle)</c> — runs the send loop over all pending
    /// events (oldest first), retrying 503/network with capped backoff. Returns a heap
    /// <c>char*</c> JSON DrainReport (fields: delivered, dead_lettered, backpressure_hits,
    /// network_errors, pending_after, stop) that the caller MUST free with
    /// <see cref="CadenceStringFree"/>, or <see cref="nint.Zero"/> on error (see
    /// <see cref="CadenceCoreLastError"/>). Marshal the returned pointer with
    /// <see cref="Marshal.PtrToStringUTF8(nint)"/> before freeing.
    /// </summary>
    [LibraryImport(CoreLibrary, EntryPoint = "cadence_core_drain")]
    internal static partial nint CadenceCoreDrain(nint handle);

    /// <summary>
    /// <c>CadenceStatus cadence_core_compact(CadenceCore* handle)</c> — forces a WAL rewrite that
    /// holds only the live pending set.
    /// </summary>
    [LibraryImport(CoreLibrary, EntryPoint = "cadence_core_compact")]
    internal static partial CadenceStatus CadenceCoreCompact(nint handle);

    /// <summary>
    /// <c>char* cadence_core_last_error(void)</c> — the current thread's last-error as a heap
    /// <c>char*</c> <c>{"code":int,"message":string}</c> JSON string (free it with
    /// <see cref="CadenceStringFree"/>), or <see cref="nint.Zero"/> if none. Reading clears it.
    /// Marshal with <see cref="Marshal.PtrToStringUTF8(nint)"/> before freeing.
    /// </summary>
    [LibraryImport(CoreLibrary, EntryPoint = "cadence_core_last_error")]
    internal static partial nint CadenceCoreLastError();

    /// <summary>
    /// <c>void cadence_string_free(char* s)</c> — frees any <c>char*</c> the core returned
    /// (dedupe id, drain report, last error). NULL-safe.
    /// </summary>
    [LibraryImport(CoreLibrary, EntryPoint = "cadence_string_free")]
    internal static partial void CadenceStringFree(nint s);

    /// <summary>
    /// <c>const char* cadence_core_version(void)</c> — the crate version as a static,
    /// NUL-terminated string. Do NOT free it. Marshal with
    /// <see cref="Marshal.PtrToStringUTF8(nint)"/>.
    /// </summary>
    [LibraryImport(CoreLibrary, EntryPoint = "cadence_core_version")]
    internal static partial nint CadenceCoreVersion();

    /// <summary>
    /// Status codes returned across the C ABI. Discriminants match the <c>CadenceStatus</c> enum
    /// in <c>agents/core/include/cadence_agent_core.h</c> / <c>src/ffi.rs</c> exactly.
    /// </summary>
    internal enum CadenceStatus
    {
        /// <summary>Success (<c>CADENCE_OK</c>).</summary>
        Ok = 0,

        /// <summary>Bounded WAL full — backpressure; pause capture (<c>CADENCE_QUEUE_FULL</c>).</summary>
        QueueFull = 1,

        /// <summary>A NULL handle/pointer or otherwise invalid argument (<c>CADENCE_INVALID_ARG</c>).</summary>
        InvalidArg = 2,

        /// <summary>Any other failure; see <see cref="CadenceCoreLastError"/> (<c>CADENCE_ERROR</c>).</summary>
        Error = 3,
    }
}
