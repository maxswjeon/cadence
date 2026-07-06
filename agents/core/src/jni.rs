//! Thin JNI export layer for the Android agent (behind the `jni` cargo feature).
//!
//! These `Java_com_cadence_agent_core_CoreBridge_native*` symbols are what Kotlin's
//! `external fun`s in `agents/android/.../core/CoreBridge.kt` resolve to. They contain **no**
//! core logic: each one unwraps the JNI arguments, then delegates into the exact same handle
//! functions the canonical C ABI in [`crate::ffi`] uses ([`capture_impl`], [`drain_impl`],
//! [`compact_impl`], …). The handle is the same heap-allocated [`CadenceCore`], passed to
//! Kotlin as a `jlong`.
//!
//! # Error mapping
//! * `QueueFull` → throws `com.cadence.agent.core.BackpressureException`.
//! * any other failure → throws `com.cadence.agent.core.CoreException`.
//! * `nativeInit` returns `0` on failure (no throw), matching the design; the detail is
//!   readable via `nativeLastError()`.
//!
//! Every body is wrapped in [`std::panic::catch_unwind`] (unwinding across the JNI boundary is
//! UB) and every handle is null-checked.

// SAFETY POLICY: like `ffi.rs`, this module is the deliberate exception to the crate's
// `#![deny(unsafe_code)]`. It reconstitutes `&mut CadenceCore` from the `jlong` handle and
// frees the handle in `nativeShutdown`. Every `unsafe` block carries its own `// SAFETY:`.
#![allow(unsafe_code)]
// The `Java_..._native*` exports take the handle as a `jlong` the JNI runtime supplies; the
// helpers reconstitute a reference from it behind a null check. Same rationale as ffi.rs.
#![allow(clippy::not_unsafe_ptr_arg_deref)]

use jni::objects::{JClass, JString};
use jni::sys::{jboolean, jlong, jstring, JNI_FALSE, JNI_TRUE};
use jni::JNIEnv;

use std::panic::{catch_unwind, AssertUnwindSafe};

use crate::ffi::{
    capture_impl, compact_impl, drain_impl, init_from_config_json, is_full_impl,
    jni_set_last_error, jni_take_last_error, peek_last_error, pending_len_impl, CadenceCore,
    CadenceStatus, CaptureOutcome,
};

const CORE_EXCEPTION: &str = "com/cadence/agent/core/CoreException";
const BACKPRESSURE_EXCEPTION: &str = "com/cadence/agent/core/BackpressureException";

/// Reconstitute a shared reference to the handle from its `jlong`, or `None` if 0/null.
fn handle_ref<'a>(handle: jlong) -> Option<&'a CadenceCore> {
    let ptr = handle as usize as *const CadenceCore;
    if ptr.is_null() {
        None
    } else {
        // SAFETY: the contract guarantees `handle` is a pointer produced by `nativeInit`
        // (`Box::into_raw`) and still live; we take a shared `&` for read-only queries.
        Some(unsafe { &*ptr })
    }
}

/// Reconstitute a mutable reference to the handle from its `jlong`, or `None` if 0/null.
fn handle_mut<'a>(handle: jlong) -> Option<&'a mut CadenceCore> {
    let ptr = handle as usize as *mut CadenceCore;
    if ptr.is_null() {
        None
    } else {
        // SAFETY: `handle` is a live, non-null pointer from `nativeInit`, not shared across
        // threads for the duration of this call (the Kotlin side serializes access), so a
        // unique `&mut` is sound.
        Some(unsafe { &mut *ptr })
    }
}

fn throw_core(env: &mut JNIEnv, msg: &str) {
    let _ = env.throw_new(CORE_EXCEPTION, msg);
}

fn throw_backpressure(env: &mut JNIEnv, msg: &str) {
    let _ = env.throw_new(BACKPRESSURE_EXCEPTION, msg);
}

/// Read a Java string argument into an owned Rust `String`.
fn read_jstring(env: &mut JNIEnv, s: &JString) -> Result<String, String> {
    env.get_string(s)
        .map(|js| js.to_string_lossy().into_owned())
        .map_err(|e| format!("invalid java string argument: {e}"))
}

/// `com.cadence.agent.core.CoreBridge.nativeInit(String) -> long` (0 on failure).
#[no_mangle]
pub extern "system" fn Java_com_cadence_agent_core_CoreBridge_nativeInit<'local>(
    mut env: JNIEnv<'local>,
    _class: JClass<'local>,
    config_json: JString<'local>,
) -> jlong {
    catch_unwind(AssertUnwindSafe(|| {
        let json = match read_jstring(&mut env, &config_json) {
            Ok(s) => s,
            Err(msg) => {
                jni_set_last_error(CadenceStatus::InvalidArg, msg);
                return 0;
            }
        };
        match init_from_config_json(&json) {
            Ok(handle) => {
                let raw = std::boxed::Box::into_raw(handle);
                raw as usize as jlong
            }
            Err(msg) => {
                jni_set_last_error(CadenceStatus::Error, msg);
                0
            }
        }
    }))
    .unwrap_or_else(|_| {
        jni_set_last_error(CadenceStatus::Error, "nativeInit: panic");
        0
    })
}

/// `nativeCapture(long, String) -> String` — dedupe id; throws on backpressure/error.
#[no_mangle]
pub extern "system" fn Java_com_cadence_agent_core_CoreBridge_nativeCapture<'local>(
    mut env: JNIEnv<'local>,
    _class: JClass<'local>,
    handle: jlong,
    envelope_json: JString<'local>,
) -> jstring {
    catch_unwind(AssertUnwindSafe(|| {
        let core = match handle_mut(handle) {
            Some(c) => c,
            None => {
                throw_core(&mut env, "nativeCapture: null handle");
                return std::ptr::null_mut();
            }
        };
        let json = match read_jstring(&mut env, &envelope_json) {
            Ok(s) => s,
            Err(msg) => {
                throw_core(&mut env, &msg);
                return std::ptr::null_mut();
            }
        };
        match capture_impl(core, &json) {
            CaptureOutcome::Ok(id) => match env.new_string(&id) {
                Ok(s) => s.into_raw(),
                Err(e) => {
                    throw_core(&mut env, &format!("nativeCapture: alloc failed: {e}"));
                    std::ptr::null_mut()
                }
            },
            CaptureOutcome::QueueFull => {
                let msg = peek_last_error().unwrap_or_else(|| "queue full".into());
                throw_backpressure(&mut env, &msg);
                std::ptr::null_mut()
            }
            CaptureOutcome::Error => {
                let msg = peek_last_error().unwrap_or_else(|| "capture failed".into());
                throw_core(&mut env, &msg);
                std::ptr::null_mut()
            }
        }
    }))
    .unwrap_or_else(|_| {
        jni_set_last_error(CadenceStatus::Error, "nativeCapture: panic");
        std::ptr::null_mut()
    })
}

/// `nativePendingLen(long) -> long` (0 on a null handle).
#[no_mangle]
pub extern "system" fn Java_com_cadence_agent_core_CoreBridge_nativePendingLen<'local>(
    _env: JNIEnv<'local>,
    _class: JClass<'local>,
    handle: jlong,
) -> jlong {
    catch_unwind(AssertUnwindSafe(|| match handle_ref(handle) {
        Some(core) => pending_len_impl(core) as jlong,
        None => 0,
    }))
    .unwrap_or(0)
}

/// `nativeIsFull(long) -> boolean` (false on a null handle).
#[no_mangle]
pub extern "system" fn Java_com_cadence_agent_core_CoreBridge_nativeIsFull<'local>(
    _env: JNIEnv<'local>,
    _class: JClass<'local>,
    handle: jlong,
) -> jboolean {
    catch_unwind(AssertUnwindSafe(|| match handle_ref(handle) {
        Some(core) if is_full_impl(core) => JNI_TRUE,
        _ => JNI_FALSE,
    }))
    .unwrap_or(JNI_FALSE)
}

/// `nativeDrain(long) -> String` — DrainReport JSON; throws `CoreException` on error.
#[no_mangle]
pub extern "system" fn Java_com_cadence_agent_core_CoreBridge_nativeDrain<'local>(
    mut env: JNIEnv<'local>,
    _class: JClass<'local>,
    handle: jlong,
) -> jstring {
    catch_unwind(AssertUnwindSafe(|| {
        let core = match handle_mut(handle) {
            Some(c) => c,
            None => {
                throw_core(&mut env, "nativeDrain: null handle");
                return std::ptr::null_mut();
            }
        };
        match drain_impl(core) {
            Ok(json) => match env.new_string(&json) {
                Ok(s) => s.into_raw(),
                Err(e) => {
                    throw_core(&mut env, &format!("nativeDrain: alloc failed: {e}"));
                    std::ptr::null_mut()
                }
            },
            Err(msg) => {
                throw_core(&mut env, &msg);
                std::ptr::null_mut()
            }
        }
    }))
    .unwrap_or_else(|_| {
        jni_set_last_error(CadenceStatus::Error, "nativeDrain: panic");
        std::ptr::null_mut()
    })
}

/// `nativeCompact(long)` — throws `CoreException` on error.
#[no_mangle]
pub extern "system" fn Java_com_cadence_agent_core_CoreBridge_nativeCompact<'local>(
    mut env: JNIEnv<'local>,
    _class: JClass<'local>,
    handle: jlong,
) {
    let _ = catch_unwind(AssertUnwindSafe(|| {
        let core = match handle_mut(handle) {
            Some(c) => c,
            None => {
                throw_core(&mut env, "nativeCompact: null handle");
                return;
            }
        };
        if compact_impl(core) != CadenceStatus::Ok {
            let msg = peek_last_error().unwrap_or_else(|| "compact failed".into());
            throw_core(&mut env, &msg);
        }
    }));
}

/// `nativeShutdown(long)` — compacts and frees the handle. Null-safe.
#[no_mangle]
pub extern "system" fn Java_com_cadence_agent_core_CoreBridge_nativeShutdown<'local>(
    _env: JNIEnv<'local>,
    _class: JClass<'local>,
    handle: jlong,
) {
    let _ = catch_unwind(AssertUnwindSafe(|| {
        let ptr = handle as usize as *mut CadenceCore;
        if ptr.is_null() {
            return;
        }
        // SAFETY: `ptr` is non-null and, per the contract, was produced by `nativeInit`
        // (`Box::into_raw`) and not yet freed. Reclaiming the Box frees it exactly once.
        let mut boxed = unsafe { std::boxed::Box::from_raw(ptr) };
        boxed.compact_best_effort();
        drop(boxed);
    }));
}

/// `nativeLastError() -> String?` — the thread-local last-error JSON (taken/cleared), or null.
#[no_mangle]
pub extern "system" fn Java_com_cadence_agent_core_CoreBridge_nativeLastError<'local>(
    env: JNIEnv<'local>,
    _class: JClass<'local>,
) -> jstring {
    catch_unwind(AssertUnwindSafe(|| match jni_take_last_error() {
        Some(json) => match env.new_string(&json) {
            Ok(s) => s.into_raw(),
            Err(_) => std::ptr::null_mut(),
        },
        None => std::ptr::null_mut(),
    }))
    .unwrap_or(std::ptr::null_mut())
}
