package com.cadence.agent.core

/**
 * JNI binding surface to the Rust `cadence-agent-core` crate (`agents/core`, W2) — the
 * portable, load-bearing implementation of envelope dedupe, persistent crash-safe WAL,
 * bounded-queue backpressure, retry/backoff, and (once its `https` feature is exercised
 * outside tests) the mTLS transport client. See the architecture note in
 * `.omc/plans/cadence-milestone-2-device-agents.md`: the hard device logic is built ONCE
 * in Rust and bound via JNI (Android, here) / P-Invoke (Windows,
 * `agents/windows/Interop/CoreInterop.cs`).
 *
 * ## Honest status (verified against the real crate — not guessed)
 * `agents/core` is real and load-bearing: it builds, and exposes a genuine public Rust
 * API in `agents/core/src/agent.rs` —
 * `AgentCore::capture` / `capture_from` / `pending_len` / `is_full` / `dead_letter` /
 * `drain` / `compact` — backed by its own durable WAL (`agents/core/src/wal.rs`) and a
 * `Transport` trait with retry/backoff (`agents/core/src/agent.rs::RetryPolicy`).
 * `Cargo.toml` already emits a `cdylib` (`crate-type = ["rlib", "cdylib"]`), so FFI
 * linking is possible in principle. **But there is no `#[no_mangle] extern "C"` export
 * layer yet** — a repo-wide grep for `no_mangle`/`extern "C"` under `agents/core/src`
 * finds nothing, so there is no real JNI symbol table to bind against yet, and no
 * compiled `.so` is packaged under `app/src/main/jniLibs/`.
 *
 * Every `external fun` below is this skeleton's best-effort mapping of the *real* Rust
 * API above onto a plausible JNI surface — not a confirmed contract. TODO(contract):
 * once a follow-up to W2 publishes
 * `#[no_mangle] extern "C" fn Java_com_cadence_agent_core_CoreBridge_...` exports,
 * reconcile every signature/name here against them (including how a Rust
 * `Result<_, AgentError>` crosses the JNI boundary — sentinel return, out-param, or a
 * thrown Java exception convention is still an open question) and delete this notice.
 * Calling any of these before that `.so` is linked throws `UnsatisfiedLinkError`.
 *
 * JNI naming convention reference:
 * https://docs.oracle.com/en/java/javase/17/docs/specs/jni/design.html#resolving-native-method-names
 */
object CoreBridge {
    init {
        // TODO(device): System.loadLibrary("cadence_agent_core") once a real .so with an
        // extern "C" export layer is packaged under app/src/main/jniLibs/<abi>/ — see the
        // externalNativeBuild TODO in app/build.gradle.kts.
    }

    /**
     * Durably appends one JSON-encoded [com.cadence.agent.envelope.EventEnvelope] to the
     * core's WAL, mirroring `AgentCore::capture`. Returns the envelope's `dedupe_id` on
     * success. TODO(contract): the real Rust signature returns
     * `Result<String, AgentError>` (`AgentError::QueueFull` when the bounded WAL is
     * full, mapping to backpressure on the caller — see [isFull]) — unresolved how that
     * crosses the JNI boundary until W2 exports it (see class doc).
     */
    external fun capture(envelopeJson: String): String

    /** Mirrors `AgentCore::pending_len` — count of un-acked events buffered in the WAL. */
    external fun pendingLen(): Long

    /** Mirrors `AgentCore::is_full` — true when the bounded WAL is full; caller must pause capture. */
    external fun isFull(): Boolean

    /**
     * Mirrors `AgentCore::drain` — attempts delivery of all pending events oldest-first
     * against the brain (`POST /ingest/event`), retrying `503`/network failures with
     * capped backoff (`RetryPolicy`). Returns a JSON-encoded `DrainReport` (`delivered`,
     * `dead_lettered`, `backpressure_hits`, `network_errors`, `pending_after`, `stop` —
     * see `agents/core/src/agent.rs::DrainReport`).
     */
    external fun drain(): String

    /** Mirrors `AgentCore::compact` — forces a WAL rewrite holding only live pending events. */
    external fun compact(): Unit
}
