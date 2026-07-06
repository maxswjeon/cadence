# cadence-agent-core — FFI Binding Surface (design)

The security-critical device logic (WAL, dedupe, backpressure, retry, mTLS transport) lives ONCE in this Rust crate. Android (JNI) and Windows (P/Invoke) bind to it. This defines the canonical binding surface so the two agents stop guessing (`TODO(contract)`).

## Layering
1. **Canonical C ABI** (`src/ffi.rs`, `#[no_mangle] pub extern "C"`) — the single source of truth. Windows P/Invoke binds to it **directly, 1:1**. Any C consumer uses it. Documented by a hand-written C header `include/cadence_agent_core.h`.
2. **JNI thin layer** (`src/jni.rs`, behind an optional `jni` cargo feature, `#[no_mangle] pub extern "system" fn Java_com_cadence_agent_core_CoreBridge_*`) — delegates into the same handle logic so Android's Kotlin `external fun`s resolve. No duplicated logic.

## Handle model (both platforms)
Opaque, heap-allocated `CadenceCore` = a wrapper over `AgentCore<Box<dyn Transport>>`. `init` returns `*mut CadenceCore` (null on failure). All other calls take the handle. `shutdown` frees it (compacts/flushes first). This requires a small, clean core addition: `impl<T: Transport + ?Sized> Transport for Box<T>` so `AgentCore<Box<dyn Transport>>` works, and it makes the FFI unit-testable by injecting a `MockTransport`.

## Error convention (resolves the stubs' open question)
Rust `Result<_, AgentError>` crosses the boundary as: **status code return** + a **thread-local last-error**.
- `CadenceStatus` (i32): `Ok=0, QueueFull=1, InvalidArg=2, Error=3`.
- Functions that return data (`init`, `drain`, `capture`'s dedupe id) return a pointer/handle, null on failure.
- On any failure the fn sets a thread-local last-error (JSON `{code,message}`); `cadence_core_last_error()` returns a freshly-allocated copy (caller frees), or null if none.
- **Every `extern` fn wraps its body in `std::panic::catch_unwind`** (unwinding across FFI is UB) → maps a panic to `Error` + last-error.
- Every pointer arg is null-checked → `InvalidArg`.

## Memory ownership
All strings the core returns are heap-allocated UTF-8 C strings owned by the **caller**, freed via `cadence_string_free`. All strings the caller passes in are borrowed (core copies what it needs). `shutdown` is the only thing that frees a handle.

## Capture vs send (correctness fix over the Windows placeholder)
The old `CoreInterop` conflated them. Reality: **capture = local WAL append** (`Ok` or `QueueFull`); **drain = the send loop** (returns per-outcome counts). So:
- `capture` returns `CadenceStatus` (Ok/QueueFull) + out-param dedupe_id string. `QueueFull` is the backpressure signal (mirrors, but is distinct from, the brain's 503).
- brain-side 202/200/503/422 outcomes live in the **DrainReport JSON** from `drain`.

## C ABI (canonical)
```c
typedef struct CadenceCore CadenceCore;
typedef enum { CADENCE_OK=0, CADENCE_QUEUE_FULL=1, CADENCE_INVALID_ARG=2, CADENCE_ERROR=3 } CadenceStatus;

// lifecycle
CadenceCore* cadence_core_init(const char* config_json);        // null on failure (see last_error)
void         cadence_core_shutdown(CadenceCore* h);             // compacts, frees; null-safe

// capture (local WAL append)
CadenceStatus cadence_core_capture(CadenceCore* h, const char* envelope_json, char** out_dedupe_id);
                                                               // Ok → *out_dedupe_id = heap string (free it); QueueFull → backpressure

// query
uint64_t      cadence_core_pending_len(const CadenceCore* h);   // 0 on null
int32_t       cadence_core_is_full(const CadenceCore* h);       // 0/1, -1 on null

// delivery
char*         cadence_core_drain(CadenceCore* h);               // JSON DrainReport (free it); null on error
CadenceStatus cadence_core_compact(CadenceCore* h);

// diagnostics + memory
char*         cadence_core_last_error(void);                    // JSON {code,message} or null; free it
void          cadence_string_free(char* s);                     // frees any char* the core returned
const char*   cadence_core_version(void);                       // static, do NOT free (crate version)
```

## init config JSON (maps to `HttpsTransport::new` + `AgentCore::open`)
```json
{
  "wal_path": "/data/data/com.cadence.agent/files/cadence.wal",
  "capacity": 10000,
  "base_url": "https://brain.local:8443",
  "client_identity_pem": "-----BEGIN CERTIFICATE-----\n...client cert + private key...\n",
  "ca_pem": "-----BEGIN CERTIFICATE-----\n...pinned brain CA...\n",
  "retry": { "base_ms": 200, "max_ms": 30000, "max_attempts": 6 }
}
```
`retry` optional (defaults to `RetryPolicy::default()`). PEMs are inline strings (agent reads its cert files and passes contents).

## JNI layer (Android, `jni` feature)
`CoreBridge.kt` moves to a **handle model** (`private var handle: Long`). Exports:
`Java_..._nativeInit(env, clazz, String configJson) -> jlong` (0 on failure),
`nativeCapture(jlong, String) -> String` (dedupe_id; throws a Kotlin `BackpressureException` on QueueFull, `CoreException` on error),
`nativePendingLen(jlong) -> jlong`, `nativeIsFull(jlong) -> jboolean`,
`nativeDrain(jlong) -> String` (DrainReport JSON), `nativeCompact(jlong)`, `nativeShutdown(jlong)`,
`nativeLastError() -> String?`. Each delegates to the same internal handle functions as the C ABI.

## Acceptance
- `cargo build` (default) + `cargo build --features jni` both compile.
- `cargo test` green incl. **new `src/ffi.rs` tests** that call the extern fns through raw pointers with an injected `MockTransport` (init→capture→pending→is_full→drain JSON→compact→shutdown, plus: null-handle→InvalidArg, bad-JSON→Error+last_error, QueueFull at capacity, panic-safety, double-free-safe string_free, last_error round-trip).
- `cargo clippy --all-targets --features jni -- -D warnings` clean; `cargo fmt --check` clean.
- `include/cadence_agent_core.h` exists and matches the exports.
- `agents/windows/Interop/CoreInterop.cs` reconciled 1:1 to the C ABI (real names/signatures/ownership, config struct, capture-vs-drain fix, string_free, last_error) — TODO(contract) notice removed.
- `agents/android/.../core/CoreBridge.kt` reconciled to the handle model + the `Java_...` exports — TODO(contract) notice removed; `System.loadLibrary("cadence_agent_core")` wired (kept behind the existing jniLibs `TODO(device)` packaging note).
- No unsafe UB: `#![forbid(unsafe_code)]` is lifted ONLY for `ffi.rs`/`jni.rs` via `#[allow(unsafe_code)]` at those modules with a `// SAFETY:` on every unsafe block; the rest of the crate keeps `forbid`.
