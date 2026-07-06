using Cadence.WindowsAgent.Collectors;
using Cadence.WindowsAgent.Mapping;
using Cadence.WindowsAgent.Wal;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;

namespace Cadence.WindowsAgent;

/// <summary>
/// Composition root: wires the four collectors' raw signals through EventMapper into the local
/// WAL. Hosted via the .NET Generic Host so the same binary runs as a console app under a
/// debugger or as a Windows Service in production —
/// https://learn.microsoft.com/en-us/dotnet/core/extensions/windows-service
///
/// TODO(device): SetWinEventHook (used by ActiveWindowCollector, which MeetingDetector /
/// AppUsageCollector / ScreenContextCollector all subscribe through) requires its calling
/// thread to pump Win32 messages (GetMessage/DispatchMessage) to actually receive events — a
/// plain BackgroundService.ExecuteAsync loop does NOT do this. A real implementation needs a
/// dedicated thread running a Win32 message loop (or a WinForms/WPF ApplicationContext, which
/// pumps messages for you) hosting the hook, not the bare `Task.Delay` loop below. This
/// skeleton starts the collectors from ExecuteAsync's thread as a structural placeholder and
/// flags the gap rather than hand-rolling an unverified raw message loop.
/// </summary>
public sealed class CadenceAgentService : BackgroundService
{
    private readonly ILogger<CadenceAgentService> _logger;
    private readonly IEventWal _wal;
    private readonly string _deviceId;
    private readonly string _accountRef;

    private ActiveWindowCollector? _activeWindowCollector;
    private AppUsageCollector? _appUsageCollector;
    private MeetingDetector? _meetingDetector;
    private ScreenContextCollector? _screenContextCollector;

    public CadenceAgentService(ILogger<CadenceAgentService> logger, IEventWal wal)
    {
        _logger = logger;
        _wal = wal;
        // TODO(device): device_id should be a stable, non-PII per-install identifier (e.g. a
        // generated GUID persisted alongside the WAL, not the Windows machine SID/name).
        // account_ref should resolve the signed-in Cadence account, not the OS username.
        _deviceId = "TODO(device):generate-and-persist-a-stable-device-id";
        _accountRef = "TODO(device):resolve-the-cadence-account-ref";
    }

    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        _activeWindowCollector = new ActiveWindowCollector();
        _appUsageCollector = new AppUsageCollector(_activeWindowCollector);
        _meetingDetector = new MeetingDetector(_activeWindowCollector);
        _screenContextCollector = new ScreenContextCollector(_activeWindowCollector);

        _activeWindowCollector.WindowChanged += async (_, window) =>
            await AppendAsync(EventMapper.FromActiveWindow(window, _deviceId, _accountRef), stoppingToken);

        _appUsageCollector.UsageSampleReady += async (_, sample) =>
            await AppendAsync(
                EventMapper.FromAppUsageSample(sample.ProcessName, sample.ForegroundDuration, sample.WindowStart, _deviceId, _accountRef),
                stoppingToken);

        _meetingDetector.MeetingDetected += async (_, detection) =>
            await AppendAsync(
                EventMapper.FromMeetingDetected(detection.AppName, detection.WindowTitle, detection.DetectedAt, _deviceId, _accountRef),
                stoppingToken);

        // ScreenContextCollector's CaptureReady is intentionally NOT wired here: its capture
        // path is a gated stub (see Collectors/ScreenContextCollector.cs) that currently always
        // throws before producing a ScreenContextCapture, so there is nothing yet to map.

        _activeWindowCollector.Start();

        try
        {
            await Task.Delay(Timeout.Infinite, stoppingToken);
        }
        catch (OperationCanceledException)
        {
            // Expected on shutdown.
        }
    }

    private async Task AppendAsync(Models.EventEnvelope envelope, CancellationToken cancellationToken)
    {
        var result = await _wal.AppendAsync(envelope, cancellationToken);
        if (result is WalAppendResult.Backpressure)
        {
            // TODO(device): a real backpressure response should throttle/drop-and-resample at
            // the collector, not just log — mirrors the brain's 503 handling contract (W1).
            _logger.LogWarning("WAL backpressure on event kind {Kind}", envelope.Kind);
        }
    }

    public override async Task StopAsync(CancellationToken cancellationToken)
    {
        _activeWindowCollector?.Stop();
        _appUsageCollector?.FlushCurrent();
        _appUsageCollector?.Dispose();
        _meetingDetector?.Dispose();
        _screenContextCollector?.Dispose();
        _activeWindowCollector?.Dispose();
        await base.StopAsync(cancellationToken);
    }
}
