using System.Collections.Concurrent;
using System.Runtime.CompilerServices;
using Cadence.WindowsAgent.Models;

namespace Cadence.WindowsAgent.Wal;

/// <summary>
/// NOT FOR PRODUCTION USE. A bare in-process queue implementing <see cref="IEventWal"/> just
/// so this skeleton's composition root (../Program.cs) has something concrete to wire up and
/// reads as a coherent, runnable-shaped agent — not as a demonstration of durability. It has
/// none of the properties the interface documents as required: nothing here survives a process
/// restart, there is no crash-safe persistence, and Backpressure is never actually returned.
///
/// TODO(device): replace with a real implementation before this agent is anything more than a
/// skeleton — either a thin wrapper over CoreInterop (../Interop/CoreInterop.cs, once
/// agents/core/W2 ships a real cdylib to bind against) or a local SQLite/append-only-file WAL.
/// </summary>
public sealed class InMemoryEventWal : IEventWal
{
    private readonly ConcurrentQueue<EventEnvelope> _queue = new();

    public Task<WalAppendResult> AppendAsync(EventEnvelope envelope, CancellationToken cancellationToken = default)
    {
        _queue.Enqueue(envelope);
        return Task.FromResult(WalAppendResult.Accepted);
    }

    public async IAsyncEnumerable<EventEnvelope> ReplayUnacknowledgedAsync(
        [EnumeratorCancellation] CancellationToken cancellationToken = default)
    {
        // Nothing survives a restart in this placeholder, so replay is always empty.
        await Task.CompletedTask;
        yield break;
    }
}
