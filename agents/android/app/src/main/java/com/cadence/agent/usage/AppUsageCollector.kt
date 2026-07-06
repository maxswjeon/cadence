package com.cadence.agent.usage

import android.app.usage.UsageEvents
import android.app.usage.UsageStatsManager
import android.content.Context

/**
 * Reads app-foreground/background transitions via [UsageStatsManager] — powers the
 * "wandering/low-value-time" detector (consensus-plan.md §Data Sources). Requires the
 * special `android.permission.PACKAGE_USAGE_STATS` permission, which cannot be granted
 * via a runtime dialog; the user must be sent to
 * `Settings.ACTION_USAGE_ACCESS_SETTINGS` to enable "Usage access" for this app.
 *
 * Docs: https://developer.android.com/reference/android/app/usage/UsageStatsManager
 */
class AppUsageCollector(private val context: Context) {

    private val manager: UsageStatsManager
        get() = context.getSystemService(Context.USAGE_STATS_SERVICE) as UsageStatsManager

    fun pollSince(beginTimeMillis: Long, endTimeMillis: Long = System.currentTimeMillis()) {
        val events: UsageEvents = manager.queryEvents(beginTimeMillis, endTimeMillis)
        val event = UsageEvents.Event()
        while (events.hasNextEvent()) {
            events.getNextEvent(event)
            when (event.eventType) {
                UsageEvents.Event.ACTIVITY_RESUMED, UsageEvents.Event.ACTIVITY_PAUSED -> {
                    // TODO(device): map via EventMapper.fromAppUsageEvent(event.packageName,
                    // event.eventType, event.timeStamp, deviceId, accountRef) and enqueue
                    // into CaptureService's WAL.
                }
                else -> Unit
            }
        }
    }

    fun hasUsageAccess(): Boolean {
        // TODO(device): AppOpsManager#unsafeCheckOpNoThrow(OPSTR_GET_USAGE_STATS, ...) or
        // attempt a zero-range queryUsageStats() and check for a non-empty result — there
        // is no direct "isGranted" API for this special permission.
        return false
    }
}
