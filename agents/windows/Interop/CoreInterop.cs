using System.Runtime.InteropServices;

namespace Cadence.WindowsAgent.Interop;

/// <summary>
/// P/Invoke boundary to <c>cadence-agent-core</c> — the shared Rust crate (agents/core, W2 in
/// the milestone-2 plan) that owns the security-critical device logic ONCE, shared with the
/// Android agent via JNI and this agent via P/Invoke: persistent WAL + crash-safe replay,
/// dedupe-id assignment, bounded-queue backpressure, and the mTLS transport client/retry loop.
/// See .omc/plans/cadence-milestone-2-device-agents.md ("Architecture note").
///
/// HONEST STATUS: agents/core (W2) had not landed in this repo when this file was written —
/// there is no compiled cdylib and no published `extern "C"` export list to bind against.
/// Every exported symbol name, calling convention, and buffer-ownership rule below is a
/// PLACEHOLDER following the conventional shape of a Rust FFI crate (cbindgen-style
/// `extern "C"` functions, an opaque handle returned as a raw pointer, caller-owns-nothing
/// UTF-8/JSON byte buffers freed by a matching `_free` export). TODO(contract): once W2 ships,
/// replace every entry point here with its real name/signature from the crate's public
/// `#[no_mangle] pub extern "C" fn ...` surface (or its cbindgen-generated header), and delete
/// this notice.
///
/// P/Invoke pattern reference: LibraryImportAttribute (.NET 7+ source-generated marshalling,
/// recommended over DllImportAttribute for new interop code) —
/// https://learn.microsoft.com/en-us/dotnet/standard/native-interop/pinvoke-source-generation
/// and https://learn.microsoft.com/en-us/dotnet/api/system.runtime.interopservices.libraryimportattribute
/// </summary>
internal static partial class CoreInterop
{
    // TODO(contract): confirm the real library name/output artifact from the Rust crate's
    // Cargo.toml `[lib] crate-type = ["cdylib"]` (conventionally `cadence_agent_core.dll` on
    // Windows via `cargo build --release`).
    private const string CoreLibrary = "cadence_agent_core";

    /// <summary>
    /// Initializes the core (opens/creates the local WAL file, starts the transport
    /// background loop). Returns an opaque, non-null handle on success or 0 on failure.
    /// TODO(device/contract): placeholder for the real init export; likely takes a config
    /// struct (WAL path, brain endpoint, mTLS client cert path) rather than nothing.
    /// </summary>
    [LibraryImport(CoreLibrary, EntryPoint = "cadence_core_init", StringMarshalling = StringMarshalling.Utf8)]
    internal static partial nint CadenceCoreInit(string configJson);

    /// <summary>
    /// Hands one serialized EventEnvelope (UTF-8 JSON, matching contract/event-envelope.schema.json
    /// once W1 publishes it) to the core for WAL append + dedupe-id assignment + eventual send.
    /// Returns a CoreSubmitResult status code (see below) — in particular, Backpressure means the
    /// core's bounded queue is full and callers must slow down, mirroring the brain's 503.
    /// TODO(device/contract): confirm whether the core wants a length-prefixed byte* or a
    /// null-terminated UTF-8 string; JSON-over-string is this skeleton's placeholder choice.
    /// </summary>
    [LibraryImport(CoreLibrary, EntryPoint = "cadence_core_submit_event", StringMarshalling = StringMarshalling.Utf8)]
    internal static partial CoreSubmitResult CadenceCoreSubmitEvent(nint coreHandle, string eventEnvelopeJson);

    /// <summary>
    /// Releases the core handle, flushing the WAL and stopping the transport loop.
    /// TODO(device/contract): confirm shutdown is synchronous/bounded-time — a hosted-service
    /// StopAsync (see ../CadenceAgentService.cs, once written) needs a timeout around this.
    /// </summary>
    [LibraryImport(CoreLibrary, EntryPoint = "cadence_core_shutdown")]
    internal static partial void CadenceCoreShutdown(nint coreHandle);

    /// <summary>
    /// Mirrors the brain's POST /ingest/event response classes (contract/protocol.md, W1):
    /// Accepted == 202 (new), Duplicate == 200 (dedupe), Backpressure == 503.
    /// TODO(contract): confirm these map 1:1 once W1's protocol.md is final — the core may
    /// also need a distinct "queued locally, brain unreachable" status for the WAL-buffered
    /// offline case, which has no direct brain-side HTTP status.
    /// </summary>
    internal enum CoreSubmitResult
    {
        Accepted = 0,
        Duplicate = 1,
        Backpressure = 2,
        Error = 3,
    }
}
