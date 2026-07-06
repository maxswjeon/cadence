using Cadence.WindowsAgent.Models;

namespace Cadence.WindowsAgent.Collectors;

/// <summary>
/// Tracks per-process foreground dwell time, subscribing to <see cref="ActiveWindowCollector"/>'s
/// on-change stream rather than polling.
///
/// Design note: unlike Android (which has <c>UsageStatsManager</c> — a first-party system API
/// for querying historical app-usage stats, see the Android W3 skeleton), Windows has no
/// equivalent public API. There is no "ask the OS how long an app was foreground" call; Windows
/// app-usage tracking is necessarily *derived* by an agent watching foreground-change events
/// and measuring the deltas itself, which is what this collector does. That is a genuine
/// platform asymmetry, not an oversight in this skeleton.
///
/// TODO(device): unverified — never run against a real foreground-change stream (this box has
/// no .NET SDK; see ../README.md).
/// </summary>
public sealed class AppUsageCollector : IDisposable
{
    private readonly ActiveWindowCollector _activeWindowCollector;
    private readonly bool _ownsActiveWindowCollector;
    private WindowInfo? _current;
    private DateTimeOffset _currentSince;

    public event EventHandler<AppUsageSample>? UsageSampleReady;

    /// <param name="activeWindowCollector">
    /// Shares an existing collector's hook (recommended — only one EVENT_SYSTEM_FOREGROUND
    /// hook should be needed per process) if provided; otherwise owns and disposes its own.
    /// </param>
    public AppUsageCollector(ActiveWindowCollector? activeWindowCollector = null)
    {
        _ownsActiveWindowCollector = activeWindowCollector is null;
        _activeWindowCollector = activeWindowCollector ?? new ActiveWindowCollector();
        _activeWindowCollector.WindowChanged += OnWindowChanged;
    }

    public void Start()
    {
        if (_ownsActiveWindowCollector)
        {
            _activeWindowCollector.Start();
        }
    }

    private void OnWindowChanged(object? sender, WindowInfo info)
    {
        var now = DateTimeOffset.UtcNow;

        // Flush the previous window's dwell time before switching.
        if (_current is { ProcessName: { } previousProcess })
        {
            var duration = now - _currentSince;
            UsageSampleReady?.Invoke(this, new AppUsageSample(previousProcess, _currentSince, duration));
        }

        _current = info;
        _currentSince = now;
    }

    /// <summary>Flushes the in-progress sample (call on graceful shutdown so the last
    /// still-foreground app's partial dwell time is not lost). TODO(device): also needs a
    /// screen-lock/screen-off hook (e.g. WTS session notifications /
    /// SystemEvents.SessionSwitch) to stop attributing dwell time while the screen is off —
    /// not wired up in this skeleton.</summary>
    public void FlushCurrent()
    {
        if (_current is { ProcessName: { } processName })
        {
            var now = DateTimeOffset.UtcNow;
            UsageSampleReady?.Invoke(this, new AppUsageSample(processName, _currentSince, now - _currentSince));
        }
    }

    public void Dispose()
    {
        _activeWindowCollector.WindowChanged -= OnWindowChanged;
        if (_ownsActiveWindowCollector)
        {
            _activeWindowCollector.Dispose();
        }
    }
}

public sealed record AppUsageSample(string ProcessName, DateTimeOffset WindowStart, TimeSpan ForegroundDuration);
