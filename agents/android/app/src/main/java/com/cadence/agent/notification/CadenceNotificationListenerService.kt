package com.cadence.agent.notification

import android.service.notification.NotificationListenerService
import android.service.notification.StatusBarNotification

/**
 * READ-ONLY notification capture (consensus-plan.md Decision G "Passive mode" +
 * Principle 1 non-destructive invariant). Bound by the system under
 * `android.permission.BIND_NOTIFICATION_LISTENER_SERVICE` once the user grants
 * notification-listener access (Settings > Notification access).
 *
 * Docs: https://developer.android.com/reference/android/service/notification/NotificationListenerService
 *
 * ## Non-destructive invariant
 * This class MUST NEVER call the inherited `cancelNotification(...)` or
 * `snoozeNotification(...)` — or any other state-mutating API. Reading a notification
 * here must have zero observable effect on the user's device: no mark-as-read, no
 * dismiss, no mute. If a future change adds any call to those methods, it violates
 * Principle 1 of the consensus plan and must be rejected in review.
 */
class CadenceNotificationListenerService : NotificationListenerService() {

    override fun onListenerConnected() {
        super.onListenerConnected()
        // TODO(device): snapshot getActiveNotifications() once connected to backfill any
        // notifications posted before this listener bound (cold-start gap).
    }

    override fun onNotificationPosted(sbn: StatusBarNotification) {
        // TODO(device): map via EventMapper.fromNotification(sbn.packageName, sbn.key,
        // sbn.postTime, sbn.notification.category, hasReplyAction = ..., deviceId, accountRef)
        // and enqueue into CaptureService's WAL. Extract only structured fields (package,
        // category, timestamp, whether a reply/action exists) — the notification's
        // verbatim title/text is raw content and must NOT land in `structured` or
        // `summary` (raw-boundary rule; see EventMapper's class doc).
        //
        // Do NOT call cancelNotification()/snoozeNotification() here or anywhere in this
        // class — see the class-level non-destructive invariant.
    }

    override fun onNotificationRemoved(sbn: StatusBarNotification) {
        // TODO(device): optionally observe removal (e.g. user read/dismissed it
        // elsewhere) as a signal — still READ-ONLY: this only observes: it never causes
        // the removal.
    }
}
