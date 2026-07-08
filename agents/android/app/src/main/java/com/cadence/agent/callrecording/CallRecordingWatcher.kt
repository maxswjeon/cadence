package com.cadence.agent.callrecording

import android.content.Context
import android.os.FileObserver
import android.util.Log

/**
 * Watches the OEM call-recorder's output directory (Samsung/Korean-OEM native call
 * recorder — consensus-plan.md Decision D) and, once S0.5 passes, ingests newly-written
 * recording files into the audio→transcript pipeline. This class only ever WATCHES a
 * directory the OEM recorder already wrote to under participant-basis consent (통비법 —
 * the recording device is itself a call participant); Cadence never taps the call audio
 * stream directly, which avoids the Android 10+ call-recording API restrictions.
 *
 * ## S0.5-GATED (do not implement capture here)
 * Per the plan: "capture itself is S0.5-GATED" — this class may detect that a new file
 * appeared (directory diff / [FileObserver]) but must NOT open, read, hash, transcribe,
 * or upload file contents until the S0.5 compliance-controls gate has passed
 * (consensus-plan.md §S0.5, AC-5). The body below is a documented stub, not a real
 * implementation, by design.
 *
 * Docs: https://developer.android.com/reference/android/os/FileObserver
 *
 * The exact OEM recorder output path is device/OEM/Android-version specific (not a
 * stable public API) and is intentionally left unresolved here — TODO(device) to
 * determine per target device at integration time. Separately unresolved: on modern
 * scoped storage, watching another app's output directory may itself require the
 * Storage Access Framework or `MANAGE_EXTERNAL_STORAGE` rather than a plain
 * `FileObserver` on a raw path — not decided here, hence no storage permission is
 * declared in the manifest for this class yet.
 *
 * ## Honest posture (S0.5)
 * Capture stays REFUSED. [start] consults [gate] — the single S0.5 check — FIRST, and the
 * default [DeniedComplianceGate] denies, so no [FileObserver] is ever constructed and no
 * file is opened/read/hashed. Even on the hypothetical permitted branch the actual
 * watch/capture body is an explicit TODO(device): capture is unbuilt, not silently wired.
 */
class CallRecordingWatcher(
    private val context: Context,
    private val recorderDirPath: String,
    private val gate: ComplianceGate = DeniedComplianceGate,
) {

    private var observer: FileObserver? = null

    fun start() {
        // Single S0.5 consult point. Default gate is DeniedComplianceGate, so this returns
        // before any FileObserver is registered and before any file is opened/read/hashed —
        // honoring the "must NOT touch files until S0.5-pass" contract above.
        val decision = gate.capturePermitted()
        if (!decision.permitted) {
            Log.i(TAG, "call-recording watcher not started: ${decision.reason}")
            return
        }
        // TODO(device, post-S0.5-sign-off): this branch is reachable only once the brain
        // provisions a permitted decision; the real OEM-recorder watch is the
        // device-specific step that stays unbuilt here — there is NO silent capture path
        // even when permitted. It would construct a FileObserver(File(recorderDirPath),
        // FileObserver.CLOSE_WRITE) that, on a new file close-write event, ONLY records
        // (file name hash, discovered-at timestamp) — no file read — and enqueues a
        // "call_recording.discovered" event via
        // EventMapper.fromCallRecordingDiscovered(fileNameHash, discoveredAtMillis,
        // deviceId, accountRef). The transcription/ingestion step itself must ALSO check an
        // S0.5-pass flag before ever opening the file; until then this watcher is inert
        // observation only.
    }

    fun stop() {
        observer?.stopWatching()
        observer = null
    }

    private companion object {
        const val TAG = "CallRecordingWatcher"
    }
}
