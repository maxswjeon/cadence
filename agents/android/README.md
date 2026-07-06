# Cadence Android agent — Milestone 2 (W3) SOURCE SKELETON

## What this is

A Kotlin/Gradle source skeleton for the Android companion agent described in
`.omc/plans/cadence-milestone-2-device-agents.md` (W3) and
`.omc/plans/cadence-consensus-plan.md` (Component 1 Capture; Decision B KakaoTalk;
Decision D recording gating; Decision G's three execution surfaces; the Data-Sources
table's on-device rows). It captures notifications, SMS, app-usage, place-based
location/BLE context, device telemetry, and (once S0.5 passes) discovers native
call-recording files — all non-destructively — and hands normalized events to the
brain over the `contract/event-envelope.schema.json` wire contract.

## Build status — honest

**This has never been built or run.** The box this skeleton was authored in has
**JDK 17 only** — no Android SDK, no Gradle wrapper jar/distribution, no Kotlin Android
Gradle Plugin resolved from a real Maven cache, no device/emulator. Every file here is a
faithful, hand-checked-against-official-docs source skeleton, not a verified build. To
actually build this you need:

- Android SDK (compileSdk 35) + a JDK 17 (already declared in `app/build.gradle.kts`)
- The Gradle wrapper jar (`gradle wrapper` needs network; `gradle/wrapper/gradle-wrapper.properties`
  here only documents the intended distribution)
- Network access to resolve `google()`/`mavenCentral()` dependencies
- A device/emulator to exercise `NotificationListenerService`/`AccessibilityService`
  grants, `PACKAGE_USAGE_STATS` special-permission grant, SMS, BLE, and location

Do not treat anything under `app/src/main/` as "it compiles" — treat it as "this is what
the real Android APIs look like, wired the way the plan requires, with every unresolved
device-integration point marked `TODO(device)`."

## Module map

```
agents/android/
  settings.gradle.kts, build.gradle.kts, gradle/wrapper/…   — project scaffolding
  app/build.gradle.kts                                       — namespace com.cadence.agent, minSdk 26 / compileSdk 35
  app/src/main/AndroidManifest.xml                            — permissions + service declarations (see below)
  app/src/main/res/xml/accessibility_service_config.xml       — AccessibilityServiceInfo config
  app/src/main/res/values/strings.xml                         — labels + the S0.1-gated messenger package list
  app/src/main/java/com/cadence/agent/
    CadenceApplication.kt
    envelope/EventEnvelope.kt, AcquisitionTier.kt             — wire type, mirrors contract/event-envelope.schema.json
    mapper/EventMapper.kt                                     — REAL mapping logic; per-source event_id/dedupe_id/structured
    core/CoreBridge.kt                                        — JNI stub to agents/core (cadence-agent-core)
    wal/EventEntity.kt, EventDao.kt, CadenceDatabase.kt        — Room-based device-side WAL
    capture/CaptureService.kt                                 — foreground orchestrator (skeleton)
    notification/CadenceNotificationListenerService.kt        — SKELETON, real signatures, READ-ONLY invariant
    accessibility/CadenceAccessibilityService.kt               — SKELETON, real signatures, idle-room-only invariant
    sms/SmsReader.kt                                           — SKELETON (ContentObserver + backfill query)
    usage/AppUsageCollector.kt                                 — SKELETON (UsageStatsManager)
    location/LocationBleCollector.kt                           — SKELETON (FusedLocationProviderClient + BluetoothLeScanner)
    callrecording/CallRecordingWatcher.kt                       — GATED STUB (S0.5) — see below
    telemetry/DeviceTelemetry.kt                                — battery read is REAL; WAL hand-off is a TODO
```

## What's a skeleton vs. what's real

Almost everything that touches an Android system API (`NotificationListenerService`,
`AccessibilityService`, `UsageStatsManager`, `FusedLocationProviderClient`,
`BluetoothLeScanner`, `Telephony.Sms`, `FileObserver`) has a **real class/method
signature** with a `TODO(device)`-marked body — the class docs cite the exact official
doc page each signature was checked against. Three things are genuinely real, working
logic (not stubs), matching the pattern the Windows sibling skeleton
(`agents/windows/`) already established:

- `mapper/EventMapper.kt` — every `from*` method builds a complete, correct
  `EventEnvelope` (stable `event_id`, `dedupe_id` fallback hash, raw-boundary-clean
  `structured` keys) from already-collected primitive values. Nothing here needs the
  Android SDK to compile (it only imports `kotlinx.serialization`/`java.time`/
  `java.security`).
- `envelope/EventEnvelope.kt` + `AcquisitionTier.kt` — plain `@Serializable` data
  classes, field-for-field aligned with the real, landed
  `contract/event-envelope.schema.json` and `cadence/adapters/base.py::Event`.
- `telemetry/DeviceTelemetry.kt`'s `snapshot()` — a real sticky-broadcast battery read.

## Non-destructive invariants (Principle 1, consensus plan)

- **`CadenceNotificationListenerService` is READ-ONLY.** It must never call
  `cancelNotification()`/`snoozeNotification()`; the class doc says so explicitly as a
  review gate, not just a comment.
- **`CadenceAccessibilityService` is idle-room-scrape ONLY**, and only via read-oriented
  node actions (`ACTION_SCROLL_FORWARD`/`ACTION_SCROLL_BACKWARD`/`ACTION_FOCUS`) — never
  a mutating action (`ACTION_CLICK`, `ACTION_SET_TEXT`, `ACTION_DISMISS`,
  `performGlobalAction`). It is also **foreground-only by OS constraint** (an
  `AccessibilityService` only ever sees the foreground window and cannot run with the
  screen off) — every backfill happens inside a user-visible Sync Session (Decision G),
  never a hidden background job.
- **`SmsReader` is READ-ONLY** against `content://sms` — this app is not, and must not
  become, the default SMS app.
- KakaoTalk is the only messenger listed in `accessibility_service_config.xml` /
  `strings.xml`'s `accessibility_target_packages` array — LINE/KakaoWork/LINE
  Works/JANDI/Naver are added one at a time, **only after each independently passes its
  own S0.1 read-state safety probe** (consensus-plan.md).

## Gated / NOT implemented here

- **`CallRecordingWatcher`** — S0.5-GATED. It may watch the OEM call-recorder's output
  directory for new files (`FileObserver`), but its body is a documented stub: it must
  never open/read/hash/transcribe/upload a file until the plan's S0.5 compliance-controls
  gate passes for this device. No audio capture, transcription, or upload logic exists
  anywhere in this skeleton.
- **No audio/co-presence/finance capture anywhere.** Co-presence identity (voice
  speaker-ID), the office-Pi mic, phone-call transcription, and CODEF/finance data all
  live outside the Android on-device agent's scope per the plan's architecture (audio
  pipeline is server-side/office-Pi; finance is CODEF, server-side) — nothing here even
  declares those surfaces.
- **`LocationBleCollector`'s BLE scan is a context prior only**, never a person-ID
  signal — see its class doc: BLE MAC randomization makes passive third-party
  identification infeasible, and the plan's chosen co-presence identity path is audio,
  not BLE.

## A taxonomy gap found while writing this — now resolved

An earlier version of this skeleton flagged a real gap: none of
`contract/event-envelope.schema.json`'s original 8 `acquisition_tier` values cleanly
described an on-device system-API read that is neither a notification WAL nor a UI
scrape (`UsageStatsManager`, `FusedLocationProviderClient`, `BatteryManager`, the SMS
content provider). The W1 owner (`m2-1`) resolved this by adding a canonical
**`device_os_api`** value to both the schema and the brain's
`cadence.adapters.base.AcquisitionTier`. This skeleton now uses
`AcquisitionTier.DEVICE_OS_API` for `fromSms`/`fromAppUsageEvent`/`fromLocationSample`/
`fromDeviceTelemetry` in `mapper/EventMapper.kt` — the earlier `UNKNOWN` +
`TODO(contract)` placeholders are gone. `NotificationListener` capture still uses
`notification_wal`, and the (not-yet-implemented) KakaoTalk/messenger accessibility
scrape is still documented to use `scrape_nonroot`.

## One remaining gap, not fixed here (out of scope for `agents/android/`)

**Windows' `EventEnvelope.SchemaVersion` wire field.** `agents/windows/Models/EventEnvelope.cs`
declares `SchemaVersion` as a serialized record property. The real schema has
`additionalProperties: false` and does not list `schema_version` among `properties` —
serializing it as-is would be a `422` envelope-shape violation
(`contract/protocol.md` §2). This Android skeleton's `EventEnvelope.SCHEMA_VERSION`
is instead a Kotlin-only constant, never serialized, matching how the Rust core
handles the same concern (`agents/core/src/envelope.rs`).

## Docs cited (checked against, not from memory alone)

- `NotificationListenerService` — https://developer.android.com/reference/android/service/notification/NotificationListenerService
- `AccessibilityService` + config — https://developer.android.com/reference/android/accessibilityservice/AccessibilityService ,
  https://developer.android.com/guide/topics/ui/accessibility/service
- `UsageStatsManager` — https://developer.android.com/reference/android/app/usage/UsageStatsManager
- Foreground services + `foregroundServiceType` — https://developer.android.com/develop/background-work/services/foreground-services
- `Telephony.Sms` — https://developer.android.com/reference/android/provider/Telephony.Sms
- `BluetoothLeScanner`/`ScanCallback` — https://developer.android.com/reference/android/bluetooth/le/BluetoothLeScanner
- `FusedLocationProviderClient` — https://developers.google.com/android/reference/com/google/android/gms/location/FusedLocationProviderClient
- `FileObserver` — https://developer.android.com/reference/android/os/FileObserver
- Room (`@Entity`/`@Dao`/`@Database`) — https://developer.android.com/training/data-storage/room
- Battery monitoring — https://developer.android.com/training/monitoring-device-state/battery-monitoring
- JNI naming convention — https://docs.oracle.com/en/java/javase/17/docs/specs/jni/design.html#resolving-native-method-names
