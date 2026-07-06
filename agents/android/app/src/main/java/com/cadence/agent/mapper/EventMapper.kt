package com.cadence.agent.mapper

import com.cadence.agent.envelope.AcquisitionTier
import com.cadence.agent.envelope.EventEnvelope
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import java.security.MessageDigest
import java.time.Instant

/**
 * Maps this agent's already-collected signals onto [EventEnvelope]
 * (`contract/event-envelope.schema.json`). Mirrors the Windows sibling's `EventMapper`
 * (`agents/windows/Mapping/EventMapper.cs`) in spirit: every method here is real,
 * runnable mapping logic — the parts that are NOT implemented are the collectors that
 * call in (each owns actual OS capture, marked `TODO(device)` in its own file under
 * `capture/`, `notification/`, `accessibility/`, `sms/`, `usage/`, `location/`,
 * `callrecording/`, `telemetry/`). This object only ever sees already-collected,
 * already-policy-decided values — it must never widen what a collector chose to omit
 * (e.g. it will not "helpfully" accept a verbatim notification body if a collector left
 * it out for the raw-boundary rule).
 *
 * ## Acquisition-tier taxonomy (resolved)
 * [fromSms], [fromAppUsageEvent], [fromLocationSample], and [fromDeviceTelemetry] use
 * [AcquisitionTier.DEVICE_OS_API] — a direct on-device OS/system-API read
 * (`UsageStatsManager`, `FusedLocationProviderClient`, `BatteryManager`, the SMS content
 * provider), distinct from a notification-listener WAL capture
 * ([AcquisitionTier.NOTIFICATION_WAL]) or an accessibility UI scrape
 * ([AcquisitionTier.SCRAPE_NONROOT]). This closes a gap the Windows sibling agent hit
 * first and initially worked around with a non-canonical `DeviceOsApi` member
 * (`agents/windows/Models/AcquisitionTier.cs`) — see `AcquisitionTier`'s own class doc.
 */
object EventMapper {

    // Source-name convention: the schema's own worked example uses "android_notification"
    // for exactly this case; the rest of these follow the same "android_<capture-kind>"
    // shape (contract/event-envelope.schema.json `examples`).
    const val SOURCE_NOTIFICATION = "android_notification"
    const val SOURCE_SMS = "android_sms"
    const val SOURCE_APP_USAGE = "android_app_usage"
    const val SOURCE_LOCATION = "android_location"
    const val SOURCE_CALL_RECORDING = "android_call_recording"
    const val SOURCE_TELEMETRY = "android_telemetry"

    /**
     * READ-ONLY notification capture. `category`/`hasReplyAction` are structural
     * metadata; the notification's verbatim title/text is raw content and must never
     * reach this method's parameters, let alone `structured` (raw-boundary rule).
     */
    fun fromNotification(
        packageName: String,
        notificationKey: String,
        postedAtMillis: Long,
        category: String?,
        hasReplyAction: Boolean,
        deviceId: String,
        accountRef: String,
    ): EventEnvelope {
        // event_id MUST be stable/reproducible for the same source item, never a random
        // UUID (protocol.md §4) — (package, notification key, post time) is that tuple.
        val eventId = "notif:$packageName:$notificationKey:$postedAtMillis"
        return EventEnvelope(
            eventId = eventId,
            source = SOURCE_NOTIFICATION,
            accountRef = accountRef,
            acquisitionTier = AcquisitionTier.NOTIFICATION_WAL,
            kind = "device.notification",
            occurredAt = isoUtc(postedAtMillis),
            deviceId = deviceId,
            dedupeId = computeDedupeId(SOURCE_NOTIFICATION, accountRef, eventId),
            summary = category?.let { "notification from $packageName ($it)" },
            structured = jsonObjectOf(
                "app_package" to packageName,
                "has_reply_action" to hasReplyAction,
            ),
        )
    }

    /** Outbound/inbound SMS metadata only — `addressHash` must already be hashed by the caller. */
    fun fromSms(
        threadId: String,
        addressHash: String,
        direction: String,
        sentAtMillis: Long,
        deviceId: String,
        accountRef: String,
    ): EventEnvelope {
        val eventId = "sms:$threadId:$sentAtMillis:$direction"
        return EventEnvelope(
            eventId = eventId,
            source = SOURCE_SMS,
            accountRef = accountRef,
            acquisitionTier = AcquisitionTier.DEVICE_OS_API,
            kind = "device.sms",
            occurredAt = isoUtc(sentAtMillis),
            deviceId = deviceId,
            dedupeId = computeDedupeId(SOURCE_SMS, accountRef, eventId),
            structured = jsonObjectOf(
                "thread_id" to threadId,
                "address_hash" to addressHash,
                "direction" to direction,
            ),
        )
    }

    /** One `UsageEvents.Event` (ACTIVITY_RESUMED/ACTIVITY_PAUSED) from [android.app.usage.UsageStatsManager]. */
    fun fromAppUsageEvent(
        packageName: String,
        eventType: Int,
        timestampMillis: Long,
        deviceId: String,
        accountRef: String,
    ): EventEnvelope {
        val eventId = "app_usage:$packageName:$eventType:$timestampMillis"
        return EventEnvelope(
            eventId = eventId,
            source = SOURCE_APP_USAGE,
            accountRef = accountRef,
            acquisitionTier = AcquisitionTier.DEVICE_OS_API,
            kind = "device.app_usage",
            occurredAt = isoUtc(timestampMillis),
            deviceId = deviceId,
            dedupeId = computeDedupeId(SOURCE_APP_USAGE, accountRef, eventId),
            structured = jsonObjectOf(
                "app_package" to packageName,
                "event_type" to eventType,
            ),
        )
    }

    /**
     * Place-based presence prior only (S0.4) — never a person-identity signal. [placeId]
     * must already be a resolved geofence/place identifier, NOT a raw latitude/longitude
     * pair (see `LocationBleCollector`'s class doc: BLE is a context prior here, never
     * used for third-party identification).
     */
    fun fromLocationSample(
        placeId: String,
        confidence: Double?,
        timestampMillis: Long,
        deviceId: String,
        accountRef: String,
    ): EventEnvelope {
        val eventId = "location:$placeId:$timestampMillis"
        return EventEnvelope(
            eventId = eventId,
            source = SOURCE_LOCATION,
            accountRef = accountRef,
            acquisitionTier = AcquisitionTier.DEVICE_OS_API,
            kind = "device.location",
            occurredAt = isoUtc(timestampMillis),
            deviceId = deviceId,
            dedupeId = computeDedupeId(SOURCE_LOCATION, accountRef, eventId),
            confidence = confidence,
            structured = jsonObjectOf("place_id" to placeId),
        )
    }

    /**
     * Maps a call-recording FILE DISCOVERY only (S0.5-GATED — see
     * `callrecording/CallRecordingWatcher.kt`). [discoveredAtMillis] is when this
     * agent's `FileObserver` saw the file appear; NOT a transcription event. No audio
     * content or transcript ever flows through this mapper. MUST NOT be called for a
     * real file until the S0.5 compliance-controls gate has passed for this device.
     * `FILE_IMPORT` fits cleanly here (an OEM app's already-written export/log file),
     * unlike the other on-device-API sources above.
     */
    fun fromCallRecordingDiscovered(
        fileNameHash: String,
        discoveredAtMillis: Long,
        deviceId: String,
        accountRef: String,
    ): EventEnvelope {
        val eventId = "call_recording_discovered:$fileNameHash:$discoveredAtMillis"
        return EventEnvelope(
            eventId = eventId,
            source = SOURCE_CALL_RECORDING,
            accountRef = accountRef,
            acquisitionTier = AcquisitionTier.FILE_IMPORT,
            kind = "device.call_recording_discovered",
            occurredAt = isoUtc(discoveredAtMillis),
            deviceId = deviceId,
            dedupeId = computeDedupeId(SOURCE_CALL_RECORDING, accountRef, eventId),
            structured = jsonObjectOf("file_name_hash" to fileNameHash),
        )
    }

    fun fromDeviceTelemetry(
        batteryPct: Int,
        isCharging: Boolean,
        timestampMillis: Long,
        deviceId: String,
        accountRef: String,
    ): EventEnvelope {
        val eventId = "telemetry:$deviceId:$timestampMillis"
        return EventEnvelope(
            eventId = eventId,
            source = SOURCE_TELEMETRY,
            accountRef = accountRef,
            acquisitionTier = AcquisitionTier.DEVICE_OS_API,
            kind = "device.telemetry",
            occurredAt = isoUtc(timestampMillis),
            deviceId = deviceId,
            dedupeId = computeDedupeId(SOURCE_TELEMETRY, accountRef, eventId),
            structured = jsonObjectOf(
                "battery_pct" to batteryPct,
                "is_charging" to isCharging,
            ),
        )
    }

    /**
     * Mirrors the brain's `Event.with_dedupe_id()` derivation exactly:
     * `sha256("{source}|{account_ref}|{event_id}")` as lowercase hex
     * (`cadence/adapters/base.py`; documented as the fallback in `contract/protocol.md`
     * §4; also independently implemented in `agents/windows/Mapping/EventMapper.cs`).
     * This is a device-computed FALLBACK matching the brain's own default — the Rust
     * core (`agents/core`, once linked via [com.cadence.agent.core.CoreBridge]) computes
     * its own richer, additionally `kind`/`occurred_at`/`payload_hash`/`device_id`-keyed
     * dedupe id (`EventEnvelope::compute_dedupe_id` in `agents/core/src/envelope.rs`),
     * and per the contract a device-set `dedupe_id` is used verbatim by the brain — so if
     * `CoreBridge` overwrites this value before submission, that richer id wins; this
     * helper only guarantees a valid, brain-compatible id exists even if `CoreBridge` is
     * never reached.
     */
    private fun computeDedupeId(source: String, accountRef: String, eventId: String): String {
        val basis = "$source|$accountRef|$eventId"
        val digest = MessageDigest.getInstance("SHA-256").digest(basis.toByteArray(Charsets.UTF_8))
        return digest.joinToString("") { "%02x".format(it) }
    }

    private fun isoUtc(epochMillis: Long): String = Instant.ofEpochMilli(epochMillis).toString()

    private fun jsonObjectOf(vararg pairs: Pair<String, Any>): JsonObject =
        JsonObject(
            pairs.associate { (k, v) ->
                k to when (v) {
                    is String -> JsonPrimitive(v)
                    is Boolean -> JsonPrimitive(v)
                    is Int -> JsonPrimitive(v)
                    is Long -> JsonPrimitive(v)
                    is Double -> JsonPrimitive(v)
                    else -> JsonPrimitive(v.toString())
                }
            },
        )
}
