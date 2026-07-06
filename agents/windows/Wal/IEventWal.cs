using Cadence.WindowsAgent.Models;

namespace Cadence.WindowsAgent.Wal;

/// <summary>
/// Local write-ahead log abstraction that every collector writes through before an event is
/// handed to <c>CoreInterop</c> for durable persistence + mTLS delivery.
///
/// IMPORTANT (architecture note, see .omc/plans/cadence-milestone-2-device-agents.md): the
/// load-bearing, crash-safe WAL — the one that survives a process kill and replays on
/// restart — lives ONCE in the Rust core (agents/core, W2), not here. This interface exists so
/// collectors have a single, testable seam to append to (<see cref="AppendAsync"/>) without
/// depending on <c>CoreInterop</c>'s P/Invoke surface directly; a production implementation is
/// expected to be a thin wrapper that calls <c>CoreInterop.CadenceCoreSubmitEvent</c> and turns
/// its <c>CoreSubmitResult</c> into this interface's <see cref="WalAppendResult"/>. A second,
/// purely-local implementation (e.g. backed by SQLite via Microsoft.Data.Sqlite, or a simple
/// append-only file) is a reasonable interim/offline-buffer stub if the core FFI is not wired
/// up yet — either way it must never lose an event that Append reported as accepted.
///
/// TODO(device): no concrete implementation is provided in this skeleton (would need either
/// the real cadence-agent-core cdylib per platform, or a local SQLite/file WAL, to test
/// meaningfully — .NET SDK is not available in this environment either way, see ../README.md).
/// </summary>
public interface IEventWal
{
    /// <summary>
    /// Appends one envelope. Must be crash-safe from the caller's perspective: once this
    /// returns <see cref="WalAppendResult.Accepted"/> or <see cref="WalAppendResult.Duplicate"/>,
    /// the event is durably queued for delivery (or already delivered) even across a process
    /// restart. <see cref="WalAppendResult.Backpressure"/> mirrors the brain's 503 — callers
    /// (collectors) must back off/drop-and-resample rather than spin.
    /// </summary>
    Task<WalAppendResult> AppendAsync(EventEnvelope envelope, CancellationToken cancellationToken = default);

    /// <summary>
    /// Replays any WAL entries not yet acknowledged by the brain, in original append order.
    /// Called once at agent startup (crash/restart recovery) before collectors begin producing
    /// new events, so backlog delivery is ordered ahead of fresh events.
    /// </summary>
    IAsyncEnumerable<EventEnvelope> ReplayUnacknowledgedAsync(CancellationToken cancellationToken = default);
}

public enum WalAppendResult
{
    Accepted,
    Duplicate,
    Backpressure,
    Error,
}
