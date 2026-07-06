package com.cadence.agent.capture

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Intent
import android.os.IBinder
import androidx.core.app.NotificationCompat
import com.cadence.agent.wal.CadenceDatabase

/**
 * Foreground orchestrator: owns the Room WAL ([com.cadence.agent.wal.CadenceDatabase])
 * and drains it into [com.cadence.agent.core.CoreBridge], and hosts the polling
 * collectors that aren't system-bound services of their own
 * ([com.cadence.agent.sms.SmsReader]'s `ContentObserver`,
 * [com.cadence.agent.usage.AppUsageCollector], [com.cadence.agent.location.LocationBleCollector],
 * [com.cadence.agent.telemetry.DeviceTelemetry]). By contrast,
 * [com.cadence.agent.notification.CadenceNotificationListenerService] and
 * [com.cadence.agent.accessibility.CadenceAccessibilityService] are bound directly by the
 * system (see the manifest) and hand events to this service's WAL rather than running
 * inside it.
 *
 * Docs: https://developer.android.com/develop/background-work/services/foreground-services
 *
 * Declared `android:foregroundServiceType="dataSync|location|connectedDevice"` in the
 * manifest (Android 14 requires a matching `FOREGROUND_SERVICE_*` permission per type —
 * also declared there).
 */
class CaptureService : Service() {

    private lateinit var db: CadenceDatabase
    // TODO(device): private val usage = AppUsageCollector(this)
    // TODO(device): private val locationBle = LocationBleCollector(this)
    // TODO(device): private val telemetry = DeviceTelemetry(this)
    // TODO(device): private val smsReader = SmsReader(this)

    override fun onCreate() {
        super.onCreate()
        // TODO(device): db = Room.databaseBuilder(applicationContext,
        //   CadenceDatabase::class.java, CadenceDatabase.DATABASE_NAME).build()
        createNotificationChannel()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        startForeground(NOTIFICATION_ID, buildForegroundNotification())
        // TODO(device): start collectors, register SmsReader's ContentObserver, and start
        // the WAL-drain coroutine loop that calls CoreBridge.capture(envelopeJson) for
        // each newly-enqueued row (once linked) and CoreBridge.drain() periodically,
        // marking rows delivered via EventDao.markDelivered() on success (contract's
        // 202/200 — protocol.md §2). Back off on CoreBridge.isFull()/backpressure,
        // mirroring the brain's own WALBuffer (cadence/ingest/pipeline.py) — the WAL entry
        // is kept, never dropped, per protocol.md §3.
        return START_STICKY
    }

    override fun onBind(intent: Intent?): IBinder? = null

    private fun createNotificationChannel() {
        val channel = NotificationChannel(
            CHANNEL_ID, "Cadence capture", NotificationManager.IMPORTANCE_MIN,
        )
        getSystemService(NotificationManager::class.java).createNotificationChannel(channel)
    }

    private fun buildForegroundNotification(): Notification =
        NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle("Cadence is capturing context")
            .setSmallIcon(android.R.drawable.ic_menu_info_details)
            .setOngoing(true)
            .build()

    companion object {
        private const val CHANNEL_ID = "cadence_capture"
        private const val NOTIFICATION_ID = 1
    }
}
