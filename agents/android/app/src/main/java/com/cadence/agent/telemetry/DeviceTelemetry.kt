package com.cadence.agent.telemetry

import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.os.BatteryManager

/**
 * Battery/charge state — powers device-care nudges (Phase 4, consensus-plan.md). Watch
 * and laptop/earbuds telemetry are separate device agents/companion-app sources that
 * report into the same event kind (`device.telemetry`); this class covers the phone
 * side only.
 *
 * Docs: https://developer.android.com/training/monitoring-device-state/battery-monitoring
 */
class DeviceTelemetry(private val context: Context) {

    /**
     * A sticky-broadcast battery read. This is real, working logic (no device
     * capability is gated or unavailable here) — only the WAL hand-off in [toEvent] is
     * a TODO, since that's collector-orchestration wiring owned by `CaptureService`.
     */
    fun snapshot(): BatterySnapshot {
        val filter = IntentFilter(Intent.ACTION_BATTERY_CHANGED)
        val status: Intent? = context.registerReceiver(null, filter)
        val level = status?.getIntExtra(BatteryManager.EXTRA_LEVEL, -1) ?: -1
        val scale = status?.getIntExtra(BatteryManager.EXTRA_SCALE, -1) ?: -1
        val plugged = status?.getIntExtra(BatteryManager.EXTRA_PLUGGED, 0) ?: 0
        val pct = if (level >= 0 && scale > 0) (level * 100 / scale) else -1
        return BatterySnapshot(batteryPct = pct, isCharging = plugged != 0)
    }

    fun toEvent(deviceId: String, accountRef: String) {
        val snap = snapshot()
        // TODO(device): EventMapper.fromDeviceTelemetry(snap.batteryPct, snap.isCharging,
        // System.currentTimeMillis(), deviceId, accountRef), enqueue into the WAL.
    }

    data class BatterySnapshot(val batteryPct: Int, val isCharging: Boolean)
}
