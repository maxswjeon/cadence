# Cadence Windows Agent (source skeleton)

Part of Cadence's Component 1 (Capture) device-agent layer. This is the Windows counterpart to
the Android agent (`agents/android/`) — see `.omc/plans/cadence-milestone-2-device-agents.md`
(workstream W4) and `.omc/plans/cadence-consensus-plan.md` for the full architecture and
decision record this implements against.

## Honest build status

**This cannot be built or run in the environment that authored it.** There is no .NET SDK
installed here — only JDK 17, Rust/cargo, Node, and Python (see the milestone-2 plan's
"Environment reality" section). Nothing under `agents/windows/` has been compiled, restored, or
executed. Every API signature cited in the source files was checked against Microsoft Learn
documentation (linked in each file's doc comments) at write time, but that is not the same as a
green build. Treat this as a faithful, well-structured **source skeleton** — not working
software — until someone builds it on a real Windows box with the .NET 8 SDK.

To actually build it, you would need: Windows, the .NET 8 SDK (`dotnet build` from this
directory), and — for anything beyond compiling — a Windows machine to run collectors against.

## Module map

```
agents/windows/
  CadenceWindowsAgent.csproj   net8.0-windows Worker SDK project; UseWPF=true (pulls in
                               UIAutomationClient.dll for System.Windows.Automation)
  Program.cs                   Generic Host composition root (console app / Windows Service)
  CadenceAgentService.cs       BackgroundService wiring collectors -> EventMapper -> IEventWal
  Collectors/
    ActiveWindowCollector.cs   foreground-window on-change watcher (SetWinEventHook +
                               GetWindowText/GetWindowThreadProcessId; UI Automation
                               enrichment point documented, not implemented)
    AppUsageCollector.cs       per-process dwell-time aggregation, derived from
                               ActiveWindowCollector (Windows has no UsageStatsManager
                               equivalent — see the class doc comment)
    MeetingDetector.cs         Zoom/Teams/Meet foreground detection by process/title;
                               capture is GATED (see below)
    ScreenContextCollector.cs  on-change/active-window capture trigger wiring; frame capture
                               is GATED (see below)
  Interop/
    NativeMethods.cs           Win32 P/Invoke declarations (User32: GetForegroundWindow,
                               GetWindowTextW, GetWindowThreadProcessId, SetWinEventHook,
                               UnhookWinEvent)
    CoreInterop.cs              P/Invoke stub to the shared Rust cadence-agent-core (agents/core,
                               W2) — SPECULATIVE, see below
  Wal/
    IEventWal.cs                local WAL interface every collector writes through
    InMemoryEventWal.cs         NOT-FOR-PRODUCTION placeholder implementation (no durability)
  Mapping/
    EventMapper.cs              maps collector signals -> EventEnvelope (dedupe_id, provenance,
                               structured-only; per-signal Source* constants + "device.*" Kind
                               naming, reconciled against the schema + the Android sibling)
  Models/
    EventEnvelope.cs             contract-shaped wire envelope with explicit JsonPropertyName
                               wire names, reconciled field-for-field against
                               contract/event-envelope.schema.json
    AcquisitionTier.cs           mirrors the schema's closed 9-value acquisition_tier enum
                               (incl. device_os_api) with an explicit JsonConverter
    WindowInfo.cs                foreground-window snapshot DTO
```

## Architecture dependency (not yet landed)

Per the milestone-2 plan's architecture note, the security-critical device logic (persistent
WAL, dedupe IDs, backpressure, mTLS transport with replay) is meant to be built ONCE as a
portable Rust core (`agents/core/`, workstream W2) that this agent binds to via P/Invoke. **W2
had not landed in this repo when this skeleton was written** — there is no cdylib and no
published `extern "C"` export list. `Interop/CoreInterop.cs` is therefore a **speculative**
placeholder ABI (function names, calling convention, and a JSON-string wire format are all
best-guess), clearly marked with `TODO(contract)`. Once W2 ships, `CoreInterop.cs` needs to be
reconciled against the real crate surface, and `Wal/InMemoryEventWal.cs` (also a
not-for-production placeholder) should be replaced by a thin wrapper over it.

`Models/EventEnvelope.cs` and `Models/AcquisitionTier.cs` were originally built directly
against the brain's Python `Event`/`AcquisitionTier` (`cadence/adapters/base.py`) because
`contract/event-envelope.schema.json` (workstream W1) had not been published yet. **W1 has
since landed and both files have been reconciled against it** (see "Fixed on reconciliation"
below) — this dependency note is kept for history/context, not as an open item.

### Fixed on reconciliation (W1 landed; cross-review caught two real `422` bugs)

1. **Serialized `schema_version`.** `EventEnvelope` used to expose a wire property
   `SchemaVersion`, but the schema has no such per-envelope field
   (`additionalProperties: false`) — any real serializer would have 422'd. It is now a
   non-serialized `public const string SchemaVersion = "1.0.0"`, mirroring the Android
   sibling's Kotlin `SCHEMA_VERSION` companion constant and the Rust core's `SCHEMA_VERSION`.
2. **Invalid `acquisition_tier` value.** `AcquisitionTier.DeviceOsApi` was a speculative
   placeholder guess at a taxonomy gap; the schema and brain have since added a canonical
   `device_os_api` member for exactly this case, and this file's mapping/converter target that
   confirmed value now (no longer speculative).
3. **Enum-as-int by default.** Neither bug above was the whole story: without an explicit
   `JsonConverter`, `System.Text.Json` serializes a C# enum as its underlying `int` (e.g. `0`),
   not a string, which would also 422. `AcquisitionTierJsonConverter` (in `AcquisitionTier.cs`)
   fixes this with an explicit per-member string mapping — deliberately not a
   `JsonNamingPolicy`-based one, because a mechanical snake_case split gets `OAuth` wrong
   (`"o_auth"` instead of the schema's `"oauth"`).
4. **PascalCase property names.** Every `EventEnvelope` property now carries an explicit
   `[JsonPropertyName("snake_case_name")]` rather than relying on a caller-configured naming
   policy — `EventId` would otherwise serialize as `"EventId"`, not `"event_id"`.
5. **`ingested_at` null-vs-omit.** The schema types `ingested_at` as bare `"string"` (not
   nullable, unlike every other optional field here) — `contract/protocol.md` §5 says to omit
   it entirely, never send `null`. `IngestedAt` is now nullable-and-omit-on-null
   (`JsonIgnoreCondition.WhenWritingNull`) instead of always-populated.
6. **Source/Kind naming convention.** `Source` was one generic `"windows_agent"` string for
   every signal; the schema's own examples and the Android sibling's `EventMapper.kt` both use
   a per-signal source (`"android_notification"`, `"windows_active_window"`), with `Kind`
   staying platform-agnostic (`"device.app_usage"`, not `"windows.app_usage"`). `EventMapper`
   now uses `SourceActiveWindow`/`SourceAppUsage`/`SourceMeetingDetected`/`SourceScreenContext`
   and `"device.*"` kinds to match.

## Non-destructive / non-negotiable invariants encoded here

- **On-change, not continuous.** `ActiveWindowCollector` only fires on `EVENT_SYSTEM_FOREGROUND`
  — there is no polling loop, and `ScreenContextCollector` only triggers off that same signal.
  No component in this tree captures continuously or on a timer.
- **No always-on screen video.** Screen capture (when eventually implemented) is a single frame
  per foreground/window change, never a video stream.
- **Raw-boundary clean.** `EventMapper` never puts verbatim window titles, document text, or
  image bytes into `EventEnvelope.Structured`; screen-context events only ever carry a
  `RawEvidenceRef`/`PayloadHash` pointer plus a non-verbatim summary, matching the brain's D1
  raw-boundary rule (Decision F).
- **No mutation of source state.** Every collector here only *reads* OS/window state
  (`GetForegroundWindow`, `GetWindowText`, process info) — nothing in this tree clicks, types
  into, or otherwise drives another application.

## Gated / explicitly NOT implemented

These are deliberate compliance boundaries, not missing features to "finish" without further
review:

- **Meeting audio/screen capture** (`MeetingDetector.BeginMeetingCaptureAsync`) — throws
  `NotSupportedException`. Recording requires the S0.5 compliance-controls gate (participant
  confirmation, visible/audible recording-state indicator, one-tap stop, audit log) per the
  consensus plan's Decision D / Phase 5 / AC-5, none of which exist in this repo yet.
- **Screen-frame pixel capture** (`ScreenContextCollector.CaptureFrameAsync`) — throws
  `NotSupportedException`. The trigger/debounce wiring is real; the actual capture (and its
  required NAS-write/OCR/retention pipeline) is an explicit stub per the milestone-2 W4
  workstream description.
- **UI Automation enrichment** (`ActiveWindowCollector.TryEnrichWithFocusedAutomationElement`) —
  documented extension point, not implemented; this is a build-effort gap (`TODO(device)`), not
  a compliance gate.

## Known real gaps (disclosed, not papered over)

- `CadenceAgentService` starts collectors from a plain `BackgroundService.ExecuteAsync` thread,
  but `SetWinEventHook`'s out-of-context callback requires its calling thread to pump Win32
  messages (`GetMessage`/`DispatchMessage`) to actually receive events — see the `TODO(device)`
  note at the top of `CadenceAgentService.cs`. A real implementation needs a dedicated
  message-pumping thread; this skeleton does not add one.
- `AppUsageCollector` does not yet stop attributing dwell time across a screen lock/unlock
  (no `SystemEvents.SessionSwitch`/WTS session-notification hook wired up).
- `device_id`/`account_ref` resolution in `CadenceAgentService` are literal placeholder strings.
