package com.cadence.agent.core

/**
 * JNI binding surface to the Rust `cadence-agent-core` crate (`agents/core`, W2) — the
 * portable, load-bearing implementation of envelope dedupe, persistent crash-safe WAL,
 * bounded-queue backpressure, retry/backoff, and the mTLS transport client. See the
 * architecture note in `.omc/plans/cadence-milestone-2-device-agents.md`: the hard device
 * logic is built ONCE in Rust and bound via JNI (Android, here) / P-Invoke (Windows,
 * `agents/windows/Interop/CoreInterop.cs`).
 *
 * ## Contract (reconciled against the real crate)
 * W2 exports a JNI layer (`agents/core/src/jni.rs`, behind the crate's `jni` cargo feature)
 * whose `Java_com_cadence_agent_core_CoreBridge_native*` symbols this class's `external fun`s
 * resolve to. That layer contains no core logic — it delegates into the same handle functions
 * as the canonical C ABI (`agents/core/src/ffi.rs`).
 *
 * ### Handle model
 * [nativeInit] heap-allocates an opaque core and returns it as a [Long] handle (0 on failure;
 * the detail is readable via [nativeLastError]). Every other native call takes that handle;
 * [nativeShutdown] compacts and frees it. This bridge holds the single live [handle].
 *
 * ### Error convention
 * A Rust `Result<_, AgentError>` crosses the boundary as a thrown exception:
 *  - `AgentError::QueueFull` → [BackpressureException] (the bounded WAL is full; the caller
 *    must pause capture — this mirrors the brain's 503).
 *  - any other failure → [CoreException].
 * [nativeInit] is the one exception: it returns 0 rather than throwing, so callers check the
 * handle and read [nativeLastError].
 *
 * TODO(device): [System.loadLibrary] resolves `libcadence_agent_core.so`, which must be built
 * (`cargo build --release --features jni` per Android ABI) and packaged under
 * `app/src/main/jniLibs/<abi>/` — see the `externalNativeBuild`/jniLibs TODO in
 * `app/build.gradle.kts`. Until that `.so` is packaged, the `init` block throws
 * `UnsatisfiedLinkError` at class load.
 *
 * JNI naming convention reference:
 * https://docs.oracle.com/en/java/javase/17/docs/specs/jni/design.html#resolving-native-method-names
 */
object CoreBridge {
    /** Opaque pointer to the Rust `CadenceCore`, or 0 when not initialized. */
    private var handle: Long = 0

    init {
        System.loadLibrary("cadence_agent_core")
    }

    /**
     * Opens/creates the WAL and builds the mTLS transport from a UTF-8 JSON config (wal_path,
     * capacity, base_url, client_identity_pem, ca_pem, optional
     * retry{base_ms,max_ms,max_attempts}), storing the resulting [handle].
     *
     * @throws CoreException if the core could not be initialized (see [nativeLastError]).
     */
    fun init(configJson: String) {
        val h = nativeInit(configJson)
        if (h == 0L) {
            throw CoreException(nativeLastError() ?: "cadence_core_init failed")
        }
        handle = h
    }

    /**
     * Like [init], but the client private key never crosses the boundary: it stays in the
     * Android StrongBox / TEE secure element and the mTLS client-auth signature is produced
     * by [signer] (a [ClientAuthSigner], typically [StrongBoxSigner]). Used after the device
     * has been accepted (B2) and its issued client cert delivered out-of-band.
     *
     * [configJson] is the delegated-signer config shape (see
     * `agents/core/src/ffi.rs::SignerCoreConfig`): the same fields as [init]'s config **minus**
     * `client_identity_pem` and **plus** `cert_chain_pem` (the issued client certificate chain,
     * leaf first). The matching private key stays behind [signer].
     *
     * @throws CoreException if the core could not be initialized (see [nativeLastError]).
     */
    fun initWithSigner(configJson: String, signer: ClientAuthSigner) {
        val h = nativeInitWithSigner(configJson, signer)
        if (h == 0L) {
            throw CoreException(nativeLastError() ?: "cadence_core_init_with_signer failed")
        }
        handle = h
    }

    /**
     * Durably appends one JSON-encoded [com.cadence.agent.envelope.EventEnvelope] to the
     * core's WAL, returning the envelope's `dedupe_id`.
     *
     * @throws BackpressureException when the bounded WAL is full (caller must pause capture).
     * @throws CoreException on any other failure (e.g. malformed envelope JSON).
     */
    fun capture(envelopeJson: String): String = nativeCapture(handle, envelopeJson)

    /** Count of un-acked events buffered in the WAL. */
    fun pendingLen(): Long = nativePendingLen(handle)

    /** True when the bounded WAL is full; the caller must pause capture. */
    fun isFull(): Boolean = nativeIsFull(handle)

    /**
     * Attempts delivery of all pending events oldest-first against the brain
     * (`POST /ingest/event`), retrying `503`/network failures with capped backoff. Returns a
     * JSON-encoded `DrainReport` (`delivered`, `dead_lettered`, `backpressure_hits`,
     * `network_errors`, `pending_after`, `stop`).
     *
     * @throws CoreException if the drain pass could not run.
     */
    fun drain(): String = nativeDrain(handle)

    /** Forces a WAL rewrite holding only live pending events. */
    fun compact() = nativeCompact(handle)

    /** Compacts and frees the native handle. Idempotent. */
    fun shutdown() {
        if (handle != 0L) {
            nativeShutdown(handle)
            handle = 0
        }
    }

    // --- native declarations (resolve to Java_com_cadence_agent_core_CoreBridge_native* in
    //     agents/core/src/jni.rs) --------------------------------------------------------- //

    private external fun nativeInit(configJson: String): Long
    private external fun nativeInitWithSigner(configJson: String, signer: ClientAuthSigner): Long
    private external fun nativeCapture(handle: Long, envelopeJson: String): String
    private external fun nativePendingLen(handle: Long): Long
    private external fun nativeIsFull(handle: Long): Boolean
    private external fun nativeDrain(handle: Long): String
    private external fun nativeCompact(handle: Long)
    private external fun nativeShutdown(handle: Long)

    /** The thread-local last-error JSON (`{code,message}`) from the native core, or null. */
    external fun nativeLastError(): String?
}

/**
 * Thrown by [CoreBridge.capture] when the core's bounded WAL is full — backpressure. The
 * caller must stop pulling from its capture source until a [CoreBridge.drain] frees space.
 * Maps to the Rust `AgentError::QueueFull` (mirrors, but is distinct from, the brain's 503).
 */
class BackpressureException(message: String) : RuntimeException(message)

/**
 * Thrown when a native `cadence-agent-core` call fails for any reason other than backpressure
 * (maps to any non-`QueueFull` `AgentError`, a parse error, or a caught panic).
 */
class CoreException(message: String) : RuntimeException(message)
