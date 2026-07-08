/*
 * cadence_agent_core.h — canonical C ABI for the `cadence-agent-core` Rust crate.
 *
 * This header is the single source of truth both device agents bind to:
 *   - Windows (.NET) via P/Invoke, 1:1 against these symbols
 *     (agents/windows/Interop/CoreInterop.cs).
 *   - Android (Kotlin) via the JNI layer in src/jni.rs, which delegates into the same
 *     handle logic (agents/android/.../core/CoreBridge.kt).
 *
 * It is hand-written to match the `#[no_mangle] pub extern "C"` exports in src/ffi.rs.
 * Keep it in sync with that file.
 *
 * ---------------------------------------------------------------------------
 * Handle model
 *   `cadence_core_init` heap-allocates an opaque `CadenceCore` and returns a `CadenceCore*`
 *   (NULL on failure). Every other call takes that handle. `cadence_core_shutdown` compacts
 *   and frees it (and is the ONLY thing that frees a handle).
 *
 * Error convention
 *   Results cross the boundary as a `CadenceStatus` return PLUS a thread-local last-error.
 *   Data-returning calls signal failure with a NULL pointer. On any failure the last-error is
 *   set; `cadence_core_last_error()` returns (and clears) a freshly-allocated `{code,message}`
 *   JSON copy, or NULL if none.
 *
 * Memory ownership
 *   Every `char*` returned by the core is a heap UTF-8 C string owned by the CALLER and must
 *   be released with `cadence_string_free` (which is NULL-safe). Strings passed INTO the core
 *   are borrowed — the core copies what it needs. `cadence_core_version` is the one exception:
 *   it returns a static string that must NOT be freed.
 *
 * Threading
 *   The last-error is thread-local. A given handle must not be used concurrently from
 *   multiple threads without external synchronization.
 * ---------------------------------------------------------------------------
 */

#ifndef CADENCE_AGENT_CORE_H
#define CADENCE_AGENT_CORE_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Opaque handle. Only ever held behind a pointer. */
typedef struct CadenceCore CadenceCore;

/* Status codes. Discriminants match `CadenceStatus` in src/ffi.rs exactly. */
typedef enum CadenceStatus {
    CADENCE_OK = 0,          /* success */
    CADENCE_QUEUE_FULL = 1,  /* bounded WAL full — backpressure; pause capture */
    CADENCE_INVALID_ARG = 2, /* a NULL handle/pointer or invalid argument */
    CADENCE_ERROR = 3        /* any other failure — see cadence_core_last_error() */
} CadenceStatus;

/* ---- lifecycle ---------------------------------------------------------- */

/*
 * Initialize the core from a UTF-8 JSON config:
 *   {
 *     "wal_path": "...",              (string, required)
 *     "capacity": 10000,             (uint,   required)
 *     "base_url": "https://...",     (string, required)
 *     "client_identity_pem": "...",  (string, required — client cert + key PEM bundle)
 *     "ca_pem": "...",               (string, required — pinned brain CA PEM)
 *     "retry": { "base_ms": 200, "max_ms": 30000, "max_attempts": 6 }  (object, optional)
 *   }
 * Returns a non-NULL handle on success, or NULL on failure (see cadence_core_last_error()).
 */
CadenceCore *cadence_core_init(const char *config_json);

/*
 * Delegated-signer client-auth callback (hardware-keystore seam).
 *
 * Invoked during the mTLS handshake so the client private key can stay inside a secure
 * element (Windows CNG/TPM, a PKCS#11 token, …) and never be exported. The platform:
 *   - receives `ctx` (the opaque pointer passed to cadence_core_init_with_signer, unchanged),
 *   - hashes the `msg_len` bytes at `msg` with SHA-256 and signs with its P-256 key,
 *   - writes the ASN.1-DER ECDSA-P256 signature into `out_sig` (capacity `out_sig_cap`, always
 *     >= 72, the max DER length) and sets `*out_sig_len` to the byte count,
 *   - returns 0 on success, or any non-zero value to fail the handshake closed.
 *
 * NOTE: the callback may run on an internal transport thread, so `ctx` must be usable off the
 * calling thread and must outlive the handle.
 */
typedef int32_t (*CadenceSignCallback)(void *ctx,
                                       const uint8_t *msg,
                                       size_t msg_len,
                                       uint8_t *out_sig,
                                       size_t out_sig_cap,
                                       size_t *out_sig_len);

/*
 * Initialize the core like cadence_core_init, but with the client private key held behind a
 * hardware keystore: the client-auth signature is produced by `sign_callback` (given
 * `sign_ctx`) instead of an exportable PEM key. The config JSON is the same shape as
 * cadence_core_init's MINUS "client_identity_pem" and PLUS "cert_chain_pem" (the client
 * certificate chain PEM, leaf first):
 *   {
 *     "wal_path": "...", "capacity": 10000, "base_url": "https://...",
 *     "cert_chain_pem": "...",   (string, required — client cert chain PEM, NO private key)
 *     "ca_pem": "...",           (string, required — pinned brain CA PEM)
 *     "retry": { ... }           (object, optional)
 *   }
 * Returns a non-NULL handle on success, or NULL on failure (see cadence_core_last_error()).
 * Handles built this way are captured/drained/freed exactly like cadence_core_init handles.
 */
CadenceCore *cadence_core_init_with_signer(const char *config_json,
                                           CadenceSignCallback sign_callback,
                                           void *sign_ctx);

/* Compact and free the handle. NULL-safe. */
void cadence_core_shutdown(CadenceCore *handle);

/* ---- capture (local WAL append) ----------------------------------------- */

/*
 * Durably capture one serialized EventEnvelope (UTF-8 JSON).
 *   CADENCE_OK         → *out_dedupe_id is set to a caller-owned heap string
 *                        (free it with cadence_string_free).
 *   CADENCE_QUEUE_FULL → backpressure; *out_dedupe_id is left untouched.
 *   CADENCE_INVALID_ARG→ a NULL argument.
 *   CADENCE_ERROR      → e.g. malformed envelope JSON (see last_error).
 */
CadenceStatus cadence_core_capture(CadenceCore *handle,
                                   const char *envelope_json,
                                   char **out_dedupe_id);

/* ---- query -------------------------------------------------------------- */

/* Number of un-acked events buffered. Returns 0 on a NULL handle. */
uint64_t cadence_core_pending_len(const CadenceCore *handle);

/* 1 when the bounded queue is full, 0 otherwise, -1 on a NULL handle. */
int32_t cadence_core_is_full(const CadenceCore *handle);

/* ---- delivery ----------------------------------------------------------- */

/*
 * Attempt delivery of all pending events, oldest first. Returns a caller-owned JSON
 * DrainReport string (free it with cadence_string_free), or NULL on error. Shape:
 *   { "delivered": u, "dead_lettered": u, "backpressure_hits": u, "network_errors": u,
 *     "pending_after": u, "stop": "drained"|"backpressured"|"network_stalled" }
 */
char *cadence_core_drain(CadenceCore *handle);

/* Force a WAL compaction. */
CadenceStatus cadence_core_compact(CadenceCore *handle);

/* ---- diagnostics + memory ----------------------------------------------- */

/*
 * The current thread's last-error as a caller-owned `{"code":i32,"message":"..."}` JSON
 * string (free it with cadence_string_free), or NULL if none. Reading it clears it.
 */
char *cadence_core_last_error(void);

/* Free any char* the core returned (dedupe id, drain report, last error). NULL-safe. */
void cadence_string_free(char *s);

/* The crate version as a static, NUL-terminated string. Do NOT free it. */
const char *cadence_core_version(void);

#ifdef __cplusplus
} /* extern "C" */
#endif

#endif /* CADENCE_AGENT_CORE_H */
