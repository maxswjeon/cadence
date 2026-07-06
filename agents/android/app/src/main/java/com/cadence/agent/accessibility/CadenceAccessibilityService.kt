package com.cadence.agent.accessibility

import android.accessibilityservice.AccessibilityService
import android.view.accessibility.AccessibilityEvent

/**
 * Idle-room scrape ONLY (consensus-plan.md Decision B / Decision G "Sync Session").
 * Bound by the system under `android.permission.BIND_ACCESSIBILITY_SERVICE`; configured
 * via `res/xml/accessibility_service_config.xml` (`android:packageNames` — KakaoTalk only
 * until additional Korean messengers each individually pass their own S0.1 probe).
 *
 * Docs: https://developer.android.com/reference/android/accessibilityservice/AccessibilityService
 * Docs: https://developer.android.com/guide/topics/ui/accessibility/service
 *
 * ## Foreground-only reality (Architect finding, Decision B)
 * `AccessibilityService` only ever sees the FOREGROUND window and cannot run with the
 * screen off — there is no silent background scrape on this API. Every backfill here
 * happens inside a user-visible **Sync Session** (Decision G): either opportunistic
 * (KakaoTalk already open) or an explicit user-initiated backfill. This class must never
 * be wired to run as a hidden/always-on background job; no such mode exists here.
 *
 * ## Non-destructive invariant
 * Only idle (not actively-being-read) rooms are scraped, and only via read-oriented node
 * actions — `AccessibilityNodeInfo.ACTION_SCROLL_FORWARD` / `ACTION_SCROLL_BACKWARD` to
 * page through history. This service MUST NEVER perform a mutating node action: no
 * `ACTION_CLICK` on message rows, no `ACTION_SET_TEXT`, no `ACTION_DISMISS`, no
 * `performGlobalAction`. A room being marked "read" is a side effect of KakaoTalk's own
 * foreground-open behavior, not of anything this service does — S0.1 (the per-messenger
 * read-state safety probe, consensus-plan.md) is the mechanism that verifies this holds
 * per app version, not just an assertion here.
 */
class CadenceAccessibilityService : AccessibilityService() {

    override fun onServiceConnected() {
        super.onServiceConnected()
        // TODO(device): confirm serviceInfo.packageNames matches the S0.1-passed
        // messenger set for this build and fail closed (self-disable) if a targeted
        // package hasn't passed S0.1 — selector-drift auto-disable per the plan's Risk
        // table ("KakaoTalk scrape marks-read / breaks on update").
    }

    override fun onAccessibilityEvent(event: AccessibilityEvent) {
        // TODO(device): on TYPE_WINDOW_CONTENT_CHANGED / TYPE_WINDOW_STATE_CHANGED for an
        // idle room, walk the node tree (read-only) and scroll back through history,
        // mapping scraped rows via a future EventMapper.fromMessengerRoom(...)-style
        // method. Enforce the forbidden-API assert from S0.1 by routing all node actions
        // through a helper that whitelists only ACTION_SCROLL_FORWARD /
        // ACTION_SCROLL_BACKWARD / ACTION_FOCUS / ACTION_NEXT_AT_MOVEMENT_GRANULARITY —
        // see the class-level non-destructive invariant.
    }

    override fun onInterrupt() {
        // TODO(device): no-op is safe — nothing in-flight to roll back (read-only).
    }
}
