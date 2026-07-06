package com.cadence.agent.sms

import android.content.Context
import android.database.ContentObserver
import android.net.Uri
import android.os.Handler
import android.provider.Telephony

/**
 * Reads the SMS/RCS content provider (`content://sms`, `Telephony.Sms`) — requires
 * `android.permission.READ_SMS` (dangerous, runtime-requested) declared in the manifest.
 *
 * Docs: https://developer.android.com/reference/android/provider/Telephony.Sms
 *
 * Real-time capture is push-driven via a [ContentObserver] registered on
 * [Telephony.Sms.CONTENT_URI] (cheaper and more reliable than the
 * `SMS_RECEIVED_ACTION` broadcast, which is intended for default-SMS-app replacement
 * flows this agent does not participate in); a one-time backfill queries the same
 * provider for messages newer than the last-synced watermark.
 *
 * READ-ONLY: this class never writes to the provider (no marking read, no deleting) —
 * this app is not the default SMS app and must not attempt to become one.
 */
class SmsReader(private val context: Context) {

    private var observer: ContentObserver? = null

    fun start(handler: Handler) {
        val obs = object : ContentObserver(handler) {
            override fun onChange(selfChange: Boolean, uri: Uri?) {
                // TODO(device): re-query Telephony.Sms.CONTENT_URI for rows newer than the
                // last-synced watermark (the `date` column), map each via
                // EventMapper.fromSms(threadId, addressHash = hash(address), direction,
                // date, deviceId, accountRef) and enqueue into CaptureService's WAL.
                // `addressHash` MUST be a hash, not the raw phone number, before it
                // reaches `structured` — see EventMapper's raw-boundary note.
            }
        }
        observer = obs
        context.contentResolver.registerContentObserver(
            Telephony.Sms.CONTENT_URI, true, obs,
        )
    }

    fun backfill(sinceEpochMillis: Long) {
        // TODO(device): context.contentResolver.query(Telephony.Sms.CONTENT_URI,
        //   projection = arrayOf(Telephony.Sms.THREAD_ID, Telephony.Sms.ADDRESS,
        //     Telephony.Sms.DATE, Telephony.Sms.TYPE),
        //   selection = "${Telephony.Sms.DATE} > ?",
        //   selectionArgs = arrayOf(sinceEpochMillis.toString()), sortOrder = null)
    }

    fun stop() {
        observer?.let { context.contentResolver.unregisterContentObserver(it) }
        observer = null
    }
}
