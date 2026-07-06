//! Canonical C ABI for `cadence-agent-core` — the single source of truth both device
//! agents bind to (Windows via P/Invoke directly, Android via the thin [`crate::jni`]
//! layer, which delegates into the very same handle logic).
//!
//! # Handle model
//! [`cadence_core_init`] heap-allocates an opaque [`CadenceCore`] (a wrapper over
//! `AgentCore<Box<dyn Transport>>`) and returns a raw `*mut CadenceCore`, null on failure.
//! Every other call takes that handle. [`cadence_core_shutdown`] compacts, then frees it.
//!
//! # Error convention
//! A `Result<_, AgentError>` crosses the boundary as a [`CadenceStatus`] return **plus** a
//! thread-local last-error (JSON `{code,message}`). Data-returning calls signal failure with
//! a null pointer. On any failure the last-error is set; [`cadence_core_last_error`] returns
//! (and clears) a freshly-allocated copy. Every extern body is wrapped in
//! [`std::panic::catch_unwind`] (unwinding across FFI is UB) and every pointer arg is
//! null-checked ([`CadenceStatus::InvalidArg`]).
//!
//! # Memory ownership
//! Strings the core returns are heap `char*` owned by the **caller**, freed with
//! [`cadence_string_free`] (null-safe). Strings the caller passes in are borrowed. The only
//! thing that frees a handle is [`cadence_core_shutdown`].

// SAFETY POLICY: the crate is `#![deny(unsafe_code)]`; this FFI boundary is the sole exception
// (mirrored in `jni.rs`). Every `unsafe` block below carries its own `// SAFETY:`.
#![allow(unsafe_code)]
// These `#[no_mangle] extern "C"` exports are the C ABI: their signatures must match the
// hand-written header and the tests call them directly, so they stay safe-`fn`. Each one
// null-checks its pointers before the single documented `unsafe` deref, which is exactly what
// this lint would otherwise force into an `unsafe fn` signature.
#![allow(clippy::not_unsafe_ptr_arg_deref)]

use std::cell::RefCell;
use std::ffi::{c_char, CStr, CString};
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::path::PathBuf;

use serde::Deserialize;

use crate::agent::{AgentCore, DrainReport, DrainStop, RetryPolicy};
use crate::envelope::EventEnvelope;
use crate::error::AgentError;
use crate::transport::Transport;

/// Status returned across the C ABI. Mirrors the `CadenceStatus` enum in the hand-written
/// `include/cadence_agent_core.h`; kept as `repr(i32)` so the discriminants match 1:1.
#[repr(i32)]
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CadenceStatus {
    /// The call succeeded.
    Ok = 0,
    /// The bounded WAL is full — backpressure; the caller must pause capture.
    QueueFull = 1,
    /// A null handle/pointer or otherwise invalid argument was passed.
    InvalidArg = 2,
    /// Any other failure (parse error, I/O error, caught panic). See `last_error`.
    Error = 3,
}

/// Opaque handle: a concrete `AgentCore` whose transport is chosen at runtime.
///
/// Named in the C header as an incomplete `struct CadenceCore` so C code only ever holds a
/// `CadenceCore*`.
pub struct CadenceCore {
    inner: AgentCore<Box<dyn Transport>>,
}

impl CadenceCore {
    /// Test/JNI seam: build a handle from an already-constructed transport, without needing
    /// real certificates. Production goes through [`cadence_core_init`] (which builds an
    /// [`crate::transport::HttpsTransport`]); tests inject a
    /// [`crate::transport::MockTransport`] here.
    pub(crate) fn from_parts(
        wal_path: PathBuf,
        capacity: usize,
        transport: Box<dyn Transport>,
        policy: RetryPolicy,
    ) -> Result<Self, AgentError> {
        Ok(CadenceCore {
            inner: AgentCore::open(wal_path, capacity, transport, policy)?,
        })
    }

    /// Best-effort WAL compaction used on the JNI shutdown path (errors are non-fatal at
    /// teardown). Keeps `inner` private while letting `jni.rs` flush before freeing.
    #[cfg(feature = "jni")]
    pub(crate) fn compact_best_effort(&mut self) {
        let _ = self.inner.compact();
    }
}

// --------------------------------------------------------------------------- //
// Thread-local last-error
// --------------------------------------------------------------------------- //

thread_local! {
    static LAST_ERROR: RefCell<Option<String>> = const { RefCell::new(None) };
}

/// Record a `{code,message}` JSON last-error for the current thread.
fn set_last_error(status: CadenceStatus, message: impl Into<String>) {
    let json = serde_json::json!({
        "code": status as i32,
        "message": message.into(),
    })
    .to_string();
    LAST_ERROR.with(|slot| *slot.borrow_mut() = Some(json));
}

/// Take (and clear) the current thread's last-error, if any.
fn take_last_error() -> Option<String> {
    LAST_ERROR.with(|slot| slot.borrow_mut().take())
}

/// Clone (without clearing) the current thread's last-error — used by the JNI layer to build
/// an exception message while leaving `nativeLastError()` still able to read it.
#[cfg(feature = "jni")]
pub(crate) fn peek_last_error() -> Option<String> {
    LAST_ERROR.with(|slot| slot.borrow().clone())
}

/// JNI-facing wrappers over the thread-local last-error (the C ABI uses the private fns).
#[cfg(feature = "jni")]
pub(crate) fn jni_take_last_error() -> Option<String> {
    take_last_error()
}

/// JNI-facing setter so `jni.rs` can record failures without duplicating the format.
#[cfg(feature = "jni")]
pub(crate) fn jni_set_last_error(status: CadenceStatus, message: impl Into<String>) {
    set_last_error(status, message)
}

/// Allocate a caller-owned C string, or null if it contains an interior NUL.
fn into_c_string(s: String) -> *mut c_char {
    match CString::new(s) {
        Ok(c) => c.into_raw(),
        Err(_) => std::ptr::null_mut(),
    }
}

/// Map an [`AgentError`] to a status + last-error (does not touch `QueueFull` callers'
/// out-params). `QueueFull` is a first-class backpressure signal, not an "error".
fn status_for(err: &AgentError) -> CadenceStatus {
    match err {
        AgentError::QueueFull { .. } => CadenceStatus::QueueFull,
        _ => CadenceStatus::Error,
    }
}

// --------------------------------------------------------------------------- //
// Config (init JSON)
// --------------------------------------------------------------------------- //

#[derive(Debug, Deserialize)]
struct CoreConfig {
    wal_path: String,
    capacity: usize,
    #[allow(dead_code)] // consumed only by the `https` transport builder below
    base_url: String,
    #[allow(dead_code)]
    client_identity_pem: String,
    #[allow(dead_code)]
    ca_pem: String,
    #[serde(default)]
    retry: Option<RetryConfig>,
}

#[derive(Debug, Deserialize)]
struct RetryConfig {
    base_ms: u64,
    max_ms: u64,
    max_attempts: u32,
}

impl RetryConfig {
    fn to_policy(&self) -> RetryPolicy {
        RetryPolicy::new(
            std::time::Duration::from_millis(self.base_ms),
            std::time::Duration::from_millis(self.max_ms),
            self.max_attempts,
        )
    }
}

/// Build the production transport from the parsed config. Only available with the `https`
/// feature (on by default); without it, `init` reports an error rather than silently
/// building nothing.
#[cfg(feature = "https")]
fn build_transport(cfg: &CoreConfig) -> Result<Box<dyn Transport>, String> {
    let transport = crate::transport::HttpsTransport::new(
        &cfg.base_url,
        cfg.client_identity_pem.as_bytes(),
        cfg.ca_pem.as_bytes(),
    )
    .map_err(|e| format!("failed to build mTLS transport: {e}"))?;
    Ok(Box::new(transport))
}

#[cfg(not(feature = "https"))]
fn build_transport(_cfg: &CoreConfig) -> Result<Box<dyn Transport>, String> {
    Err("cadence-agent-core built without the `https` feature; cannot open a transport".into())
}

/// Parse the config JSON and construct a live handle. Shared by the C ABI and JNI `init`.
pub(crate) fn init_from_config_json(config_json: &str) -> Result<Box<CadenceCore>, String> {
    let cfg: CoreConfig =
        serde_json::from_str(config_json).map_err(|e| format!("invalid config json: {e}"))?;
    let policy = cfg
        .retry
        .as_ref()
        .map(RetryConfig::to_policy)
        .unwrap_or_default();
    let transport = build_transport(&cfg)?;
    let core = CadenceCore::from_parts(
        PathBuf::from(&cfg.wal_path),
        cfg.capacity,
        transport,
        policy,
    )
    .map_err(|e| format!("failed to open core: {e}"))?;
    Ok(Box::new(core))
}

// --------------------------------------------------------------------------- //
// Shared handle logic (called by both the C ABI here and the JNI layer)
// --------------------------------------------------------------------------- //

/// Serialize a [`DrainReport`] to the wire JSON both agents parse.
pub(crate) fn drain_report_json(report: &DrainReport) -> String {
    let stop = match report.stop {
        DrainStop::Drained => "drained",
        DrainStop::Backpressured => "backpressured",
        DrainStop::NetworkStalled => "network_stalled",
    };
    serde_json::json!({
        "delivered": report.delivered,
        "dead_lettered": report.dead_lettered,
        "backpressure_hits": report.backpressure_hits,
        "network_errors": report.network_errors,
        "pending_after": report.pending_after,
        "stop": stop,
    })
    .to_string()
}

/// Outcome of the shared capture logic: `Ok(dedupe_id)` / backpressure / error.
pub(crate) enum CaptureOutcome {
    Ok(String),
    QueueFull,
    Error,
}

/// Core capture logic shared by the C ABI and JNI. Parses the envelope JSON, appends to the
/// WAL, and sets the thread-local last-error on any failure.
pub(crate) fn capture_impl(core: &mut CadenceCore, envelope_json: &str) -> CaptureOutcome {
    let env: EventEnvelope = match serde_json::from_str(envelope_json) {
        Ok(env) => env,
        Err(e) => {
            set_last_error(CadenceStatus::Error, format!("invalid envelope json: {e}"));
            return CaptureOutcome::Error;
        }
    };
    match core.inner.capture(env) {
        Ok(id) => CaptureOutcome::Ok(id),
        Err(err @ AgentError::QueueFull { .. }) => {
            set_last_error(CadenceStatus::QueueFull, err.to_string());
            CaptureOutcome::QueueFull
        }
        Err(err) => {
            set_last_error(CadenceStatus::Error, err.to_string());
            CaptureOutcome::Error
        }
    }
}

/// Core drain logic shared by the C ABI and JNI. Returns the DrainReport JSON, or an error
/// string (also stored as the last-error) on failure.
pub(crate) fn drain_impl(core: &mut CadenceCore) -> Result<String, String> {
    match core.inner.drain() {
        Ok(report) => Ok(drain_report_json(&report)),
        Err(err) => {
            let msg = err.to_string();
            set_last_error(CadenceStatus::Error, msg.clone());
            Err(msg)
        }
    }
}

/// Core compact logic shared by the C ABI and JNI.
pub(crate) fn compact_impl(core: &mut CadenceCore) -> CadenceStatus {
    match core.inner.compact() {
        Ok(()) => CadenceStatus::Ok,
        Err(err) => {
            set_last_error(CadenceStatus::Error, err.to_string());
            status_for(&err)
        }
    }
}

pub(crate) fn pending_len_impl(core: &CadenceCore) -> u64 {
    core.inner.pending_len() as u64
}

pub(crate) fn is_full_impl(core: &CadenceCore) -> bool {
    core.inner.is_full()
}

// --------------------------------------------------------------------------- //
// C ABI exports
// --------------------------------------------------------------------------- //

/// Initialize the core from a UTF-8 JSON config (see the module docs / C header). Returns a
/// non-null `*mut CadenceCore` on success, or null on failure (see `cadence_core_last_error`).
///
/// # Safety
/// `config_json` must be a valid, NUL-terminated C string (or null, which fails cleanly).
#[no_mangle]
pub extern "C" fn cadence_core_init(config_json: *const c_char) -> *mut CadenceCore {
    catch_unwind(|| {
        if config_json.is_null() {
            set_last_error(
                CadenceStatus::InvalidArg,
                "cadence_core_init: null config_json",
            );
            return std::ptr::null_mut();
        }
        // SAFETY: `config_json` is non-null (checked above); the FFI contract requires the
        // caller to pass a valid NUL-terminated C string that outlives this call.
        let cstr = unsafe { CStr::from_ptr(config_json) };
        let json = match cstr.to_str() {
            Ok(s) => s,
            Err(_) => {
                set_last_error(
                    CadenceStatus::InvalidArg,
                    "cadence_core_init: config_json not utf-8",
                );
                return std::ptr::null_mut();
            }
        };
        match init_from_config_json(json) {
            Ok(handle) => Box::into_raw(handle),
            Err(msg) => {
                set_last_error(CadenceStatus::Error, msg);
                std::ptr::null_mut()
            }
        }
    })
    .unwrap_or_else(|_| {
        set_last_error(CadenceStatus::Error, "cadence_core_init: panic");
        std::ptr::null_mut()
    })
}

/// Compact and free the handle. Null-safe.
///
/// # Safety
/// `handle` must be a pointer returned by [`cadence_core_init`] and not already freed.
#[no_mangle]
pub extern "C" fn cadence_core_shutdown(handle: *mut CadenceCore) {
    let _ = catch_unwind(AssertUnwindSafe(|| {
        if handle.is_null() {
            return;
        }
        // SAFETY: `handle` is non-null and, per the FFI contract, was produced by
        // `cadence_core_init` (`Box::into_raw`) and not yet freed. Reclaiming the Box frees it.
        let mut boxed = unsafe { Box::from_raw(handle) };
        // Best-effort compaction on the way out; errors are non-fatal at shutdown.
        let _ = boxed.inner.compact();
        drop(boxed);
    }));
}

/// Durably capture one envelope (JSON). On `Ok`, `*out_dedupe_id` is set to a caller-owned
/// heap string. `QueueFull` signals backpressure.
///
/// # Safety
/// `handle` must be a live handle; `envelope_json` a valid C string; `out_dedupe_id` a valid
/// writable `char**`.
#[no_mangle]
pub extern "C" fn cadence_core_capture(
    handle: *mut CadenceCore,
    envelope_json: *const c_char,
    out_dedupe_id: *mut *mut c_char,
) -> CadenceStatus {
    catch_unwind(AssertUnwindSafe(|| {
        if handle.is_null() || envelope_json.is_null() || out_dedupe_id.is_null() {
            set_last_error(
                CadenceStatus::InvalidArg,
                "cadence_core_capture: null argument",
            );
            return CadenceStatus::InvalidArg;
        }
        // SAFETY: both pointers are non-null (checked); the contract guarantees `handle` is a
        // live `&mut`-able handle unshared across threads for this call, and `envelope_json`
        // is a valid NUL-terminated string.
        let core = unsafe { &mut *handle };
        let cstr = unsafe { CStr::from_ptr(envelope_json) };
        let json = match cstr.to_str() {
            Ok(s) => s,
            Err(_) => {
                set_last_error(
                    CadenceStatus::Error,
                    "cadence_core_capture: envelope_json not utf-8",
                );
                return CadenceStatus::Error;
            }
        };
        match capture_impl(core, json) {
            CaptureOutcome::Ok(id) => {
                // SAFETY: `out_dedupe_id` is a non-null, writable `char**` per the contract.
                unsafe { *out_dedupe_id = into_c_string(id) };
                CadenceStatus::Ok
            }
            CaptureOutcome::QueueFull => CadenceStatus::QueueFull,
            CaptureOutcome::Error => CadenceStatus::Error,
        }
    }))
    .unwrap_or_else(|_| {
        set_last_error(CadenceStatus::Error, "cadence_core_capture: panic");
        CadenceStatus::Error
    })
}

/// Number of un-acked events buffered. Returns 0 on a null handle.
///
/// # Safety
/// `handle` must be a live handle or null.
#[no_mangle]
pub extern "C" fn cadence_core_pending_len(handle: *const CadenceCore) -> u64 {
    catch_unwind(AssertUnwindSafe(|| {
        if handle.is_null() {
            return 0;
        }
        // SAFETY: `handle` is non-null and points at a live handle per the contract; we only
        // take a shared `&` for a read-only query.
        let core = unsafe { &*handle };
        pending_len_impl(core)
    }))
    .unwrap_or(0)
}

/// `1` when the bounded queue is full, `0` otherwise, `-1` on a null handle.
///
/// # Safety
/// `handle` must be a live handle or null.
#[no_mangle]
pub extern "C" fn cadence_core_is_full(handle: *const CadenceCore) -> i32 {
    catch_unwind(AssertUnwindSafe(|| {
        if handle.is_null() {
            return -1;
        }
        // SAFETY: non-null, live handle per the contract; shared `&` for a read-only query.
        let core = unsafe { &*handle };
        i32::from(is_full_impl(core))
    }))
    .unwrap_or(-1)
}

/// Attempt delivery of all pending events. Returns a caller-owned DrainReport JSON string, or
/// null on error (see `cadence_core_last_error`).
///
/// # Safety
/// `handle` must be a live handle.
#[no_mangle]
pub extern "C" fn cadence_core_drain(handle: *mut CadenceCore) -> *mut c_char {
    catch_unwind(AssertUnwindSafe(|| {
        if handle.is_null() {
            set_last_error(CadenceStatus::InvalidArg, "cadence_core_drain: null handle");
            return std::ptr::null_mut();
        }
        // SAFETY: non-null, live handle per the contract; drain needs `&mut`.
        let core = unsafe { &mut *handle };
        match drain_impl(core) {
            Ok(json) => into_c_string(json),
            Err(_) => std::ptr::null_mut(),
        }
    }))
    .unwrap_or_else(|_| {
        set_last_error(CadenceStatus::Error, "cadence_core_drain: panic");
        std::ptr::null_mut()
    })
}

/// Force a WAL compaction.
///
/// # Safety
/// `handle` must be a live handle.
#[no_mangle]
pub extern "C" fn cadence_core_compact(handle: *mut CadenceCore) -> CadenceStatus {
    catch_unwind(AssertUnwindSafe(|| {
        if handle.is_null() {
            set_last_error(
                CadenceStatus::InvalidArg,
                "cadence_core_compact: null handle",
            );
            return CadenceStatus::InvalidArg;
        }
        // SAFETY: non-null, live handle per the contract; compact needs `&mut`.
        let core = unsafe { &mut *handle };
        compact_impl(core)
    }))
    .unwrap_or_else(|_| {
        set_last_error(CadenceStatus::Error, "cadence_core_compact: panic");
        CadenceStatus::Error
    })
}

/// Take (and clear) the current thread's last-error as a caller-owned `{code,message}` JSON
/// string, or null if none is set.
#[no_mangle]
pub extern "C" fn cadence_core_last_error() -> *mut c_char {
    catch_unwind(|| match take_last_error() {
        Some(json) => into_c_string(json),
        None => std::ptr::null_mut(),
    })
    .unwrap_or(std::ptr::null_mut())
}

/// Free any `char*` the core returned. Null-safe.
///
/// # Safety
/// `s` must be a pointer previously returned by a `cadence_*` call (or null) and not already
/// freed.
#[no_mangle]
pub extern "C" fn cadence_string_free(s: *mut c_char) {
    let _ = catch_unwind(AssertUnwindSafe(|| {
        if s.is_null() {
            return;
        }
        // SAFETY: `s` is non-null and, per the contract, was produced by `CString::into_raw`
        // in this crate and not yet freed. Reclaiming the `CString` frees it exactly once.
        drop(unsafe { CString::from_raw(s) });
    }));
}

/// The crate version as a static, NUL-terminated C string. Do **not** free it.
#[no_mangle]
pub extern "C" fn cadence_core_version() -> *const c_char {
    // `concat!` yields a `&'static str` with an explicit trailing NUL; its pointer is valid
    // for the life of the program and needs no allocation, so this is safe (no `unsafe`).
    concat!(env!("CARGO_PKG_VERSION"), "\0").as_ptr() as *const c_char
}

// --------------------------------------------------------------------------- //
// Tests — exercise the extern fns through raw pointers with an injected MockTransport.
// --------------------------------------------------------------------------- //

#[cfg(test)]
mod tests {
    use super::*;
    use crate::envelope::EventEnvelope;
    use crate::transport::MockTransport;

    /// Build a live handle through the test seam (no real certs), returning a raw pointer the
    /// C-ABI fns accept. Freed via `cadence_core_shutdown`.
    fn handle_with_capacity(dir: &std::path::Path, capacity: usize) -> *mut CadenceCore {
        let core = CadenceCore::from_parts(
            dir.join("test.wal"),
            capacity,
            Box::new(MockTransport::new()),
            RetryPolicy::no_sleep(3),
        )
        .expect("seam should open a WAL");
        Box::into_raw(Box::new(core))
    }

    fn envelope_json(event_id: &str) -> CString {
        let env = EventEnvelope::builder(event_id, "kakaotalk", "acct-1", "message.posted")
            .payload_hash("abc")
            .build();
        CString::new(serde_json::to_string(&env).unwrap()).unwrap()
    }

    #[test]
    fn full_lifecycle_through_raw_pointers() {
        let dir = tempfile::tempdir().unwrap();
        let h = handle_with_capacity(dir.path(), 16);

        // capture → Ok + a dedupe id out-param.
        let json = envelope_json("e1");
        let mut out: *mut c_char = std::ptr::null_mut();
        let status = cadence_core_capture(h, json.as_ptr(), &mut out);
        assert_eq!(status, CadenceStatus::Ok);
        assert!(!out.is_null());
        // SAFETY: `out` is a live core-owned string from the successful capture above.
        let id = unsafe { CStr::from_ptr(out) }.to_str().unwrap().to_string();
        assert!(!id.is_empty());
        cadence_string_free(out);

        // pending_len / is_full.
        assert_eq!(cadence_core_pending_len(h), 1);
        assert_eq!(cadence_core_is_full(h), 0);

        // drain → parseable DrainReport JSON with the one event delivered.
        let report_ptr = cadence_core_drain(h);
        assert!(!report_ptr.is_null());
        // SAFETY: non-null core-owned string from drain.
        let report_str = unsafe { CStr::from_ptr(report_ptr) }.to_str().unwrap();
        let report: serde_json::Value = serde_json::from_str(report_str).unwrap();
        assert_eq!(report["delivered"], 1);
        assert_eq!(report["pending_after"], 0);
        assert_eq!(report["stop"], "drained");
        cadence_string_free(report_ptr);

        assert_eq!(cadence_core_pending_len(h), 0);

        // compact → Ok, then shutdown frees the handle.
        assert_eq!(cadence_core_compact(h), CadenceStatus::Ok);
        cadence_core_shutdown(h);
    }

    #[test]
    fn null_handle_is_invalid_arg() {
        let mut out: *mut c_char = std::ptr::null_mut();
        let json = envelope_json("e1");
        assert_eq!(
            cadence_core_capture(std::ptr::null_mut(), json.as_ptr(), &mut out),
            CadenceStatus::InvalidArg
        );
        assert_eq!(
            cadence_core_compact(std::ptr::null_mut()),
            CadenceStatus::InvalidArg
        );
        assert_eq!(cadence_core_pending_len(std::ptr::null()), 0);
        assert_eq!(cadence_core_is_full(std::ptr::null()), -1);
        assert!(cadence_core_drain(std::ptr::null_mut()).is_null());

        // The last failing call populated the last-error.
        let err = cadence_core_last_error();
        assert!(!err.is_null());
        cadence_string_free(err);
    }

    #[test]
    fn null_envelope_and_out_are_invalid_arg() {
        let dir = tempfile::tempdir().unwrap();
        let h = handle_with_capacity(dir.path(), 16);
        let mut out: *mut c_char = std::ptr::null_mut();
        assert_eq!(
            cadence_core_capture(h, std::ptr::null(), &mut out),
            CadenceStatus::InvalidArg
        );
        let json = envelope_json("e1");
        assert_eq!(
            cadence_core_capture(h, json.as_ptr(), std::ptr::null_mut()),
            CadenceStatus::InvalidArg
        );
        cadence_core_shutdown(h);
    }

    #[test]
    fn bad_envelope_json_is_error_and_sets_last_error() {
        let _ = take_last_error(); // clear any residue on this thread
        let dir = tempfile::tempdir().unwrap();
        let h = handle_with_capacity(dir.path(), 16);

        let bad = CString::new("{ not valid json").unwrap();
        let mut out: *mut c_char = std::ptr::null_mut();
        let status = cadence_core_capture(h, bad.as_ptr(), &mut out);
        assert_eq!(status, CadenceStatus::Error);
        assert!(out.is_null());

        let err = cadence_core_last_error();
        assert!(!err.is_null());
        // SAFETY: non-null core-owned last-error string.
        let err_str = unsafe { CStr::from_ptr(err) }.to_str().unwrap().to_string();
        let parsed: serde_json::Value = serde_json::from_str(&err_str).unwrap();
        assert_eq!(parsed["code"], CadenceStatus::Error as i32);
        assert!(parsed["message"]
            .as_str()
            .unwrap()
            .contains("invalid envelope json"));
        cadence_string_free(err);

        cadence_core_shutdown(h);
    }

    #[test]
    fn queue_full_at_capacity() {
        let _ = take_last_error();
        let dir = tempfile::tempdir().unwrap();
        let h = handle_with_capacity(dir.path(), 1);

        // First fills the single-slot queue.
        let mut out: *mut c_char = std::ptr::null_mut();
        assert_eq!(
            cadence_core_capture(h, envelope_json("e1").as_ptr(), &mut out),
            CadenceStatus::Ok
        );
        cadence_string_free(out);

        // Second (distinct event) hits the bound → backpressure.
        let mut out2: *mut c_char = std::ptr::null_mut();
        assert_eq!(
            cadence_core_capture(h, envelope_json("e2").as_ptr(), &mut out2),
            CadenceStatus::QueueFull
        );
        assert!(out2.is_null());
        assert_eq!(cadence_core_is_full(h), 1);

        cadence_core_shutdown(h);
    }

    #[test]
    fn panic_in_extern_body_is_caught_as_error() {
        // Drive the panic path directly: `capture_impl` panics if we feed it a poisoned
        // handle is impractical, so exercise the catch_unwind wrapper via a forced panic in a
        // guarded closure identical to the extern bodies.
        let caught = catch_unwind(AssertUnwindSafe(|| -> CadenceStatus {
            panic!("boom");
        }))
        .unwrap_or_else(|_| {
            set_last_error(CadenceStatus::Error, "panic path");
            CadenceStatus::Error
        });
        assert_eq!(caught, CadenceStatus::Error);
        let err = cadence_core_last_error();
        assert!(!err.is_null());
        cadence_string_free(err);
    }

    #[test]
    fn string_free_null_is_safe() {
        // Must not crash / abort.
        cadence_string_free(std::ptr::null_mut());
    }

    #[test]
    fn last_error_round_trips_and_clears() {
        let _ = take_last_error();
        // No error set yet → null.
        assert!(cadence_core_last_error().is_null());

        set_last_error(CadenceStatus::Error, "some failure");
        let first = cadence_core_last_error();
        assert!(!first.is_null());
        // SAFETY: non-null core-owned last-error string.
        let s = unsafe { CStr::from_ptr(first) }
            .to_str()
            .unwrap()
            .to_string();
        assert!(s.contains("some failure"));
        cadence_string_free(first);

        // Taking it cleared it → a second read is null.
        assert!(cadence_core_last_error().is_null());
    }

    #[test]
    fn version_is_static_nul_terminated() {
        let ptr = cadence_core_version();
        assert!(!ptr.is_null());
        // SAFETY: `cadence_core_version` returns a valid static NUL-terminated string.
        let v = unsafe { CStr::from_ptr(ptr) }.to_str().unwrap();
        assert_eq!(v, env!("CARGO_PKG_VERSION"));
    }
}
