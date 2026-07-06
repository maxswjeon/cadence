package com.cadence.agent

import android.app.Application

/**
 * Application entrypoint. TODO(device): initialize the Room WAL database, WorkManager
 * (for periodic AppUsageCollector polling / SMS backfill scheduling), and
 * [com.cadence.agent.core.CoreBridge] here once this module can actually build (needs
 * the Android SDK/Gradle + an `agents/core` `.so` with a real JNI export layer — see
 * CoreBridge's class doc).
 */
class CadenceApplication : Application() {
    override fun onCreate() {
        super.onCreate()
    }
}
