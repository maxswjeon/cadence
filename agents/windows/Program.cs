using Cadence.WindowsAgent;
using Cadence.WindowsAgent.Wal;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;

// Generic Host + UseWindowsService: runs as a normal console app under a debugger, and as a
// Windows Service (via `sc create` / an installer) in production without code changes.
// https://learn.microsoft.com/en-us/dotnet/core/extensions/windows-service
var builder = Host.CreateApplicationBuilder(args);
builder.Services.AddWindowsService(options =>
{
    options.ServiceName = "CadenceWindowsAgent";
});

// TODO(device): InMemoryEventWal is a NOT-FOR-PRODUCTION placeholder (see Wal/InMemoryEventWal.cs) —
// swap for a CoreInterop-backed IEventWal once agents/core (W2) ships a real cdylib, or a local
// SQLite/append-only-file WAL in the interim.
builder.Services.AddSingleton<IEventWal, InMemoryEventWal>();
builder.Services.AddHostedService<CadenceAgentService>();

var host = builder.Build();
host.Run();
