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

// The delegated-signer (StrongBox) path additionally needs the `https` transport it wires the
// signer into. Android builds both features; these imports and the `nativeInitWithSigner`
// export below are gated so a `--no-default-features --features jni` build still compiles.
#[cfg(feature = "https")]
use jni::objects::{GlobalRef, JByteArray, JObject, JValue};
#[cfg(feature = "https")]
use jni::JavaVM;

#[cfg(feature = "https")]
use crate::ffi::init_with_signer_from_config_json;
#[cfg(feature = "https")]
use crate::transport::{ClientAuthSigner, SignerError};

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

/// A [`ClientAuthSigner`] that calls back into a Java/Kotlin
/// `com.cadence.agent.core.ClientAuthSigner` — the JNI expression of the B1 hardware-keystore
/// seam ([`crate::ffi::CallbackSigner`] is its C-ABI sibling). The Java object wraps an
/// Android **StrongBox** `java.security.Signature("SHA256withECDSA")` whose P-256 private key
/// never leaves the secure element; only the message goes in and the DER signature comes out.
///
/// # Contract (Kotlin side)
/// ```kotlin
/// package com.cadence.agent.core
/// interface ClientAuthSigner {
///     /**
///      * SHA-256 + sign the raw TLS CertificateVerify `message` with the StrongBox P-256
///      * key, returning the ASN.1-DER ECDSA signature — i.e. exactly what
///      * `Signature.getInstance("SHA256withECDSA")` over a StrongBox `PrivateKey` produces.
///      * Throw to signal a signing failure (the mTLS handshake then fails closed).
///      */
///     fun sign(message: ByteArray): ByteArray
/// }
/// ```
///
/// **Device/emulator validation pending:** this path cannot be unit-tested without a live JVM
/// and a StrongBox key, so it is compile-checked here and exercised on-device by the Android
/// agent. The C-ABI sibling [`crate::ffi::CallbackSigner`] has full loopback-handshake test
/// coverage proving the delegated-signer seam itself is correct.
#[cfg(feature = "https")]
struct JavaSigner {
    /// Handle to the running JVM, used to attach the (reqwest-internal) signing thread.
    vm: JavaVM,
    /// Global ref to the Java `ClientAuthSigner` object (survives across threads/frames).
    signer: GlobalRef,
}

#[cfg(feature = "https")]
impl ClientAuthSigner for JavaSigner {
    fn sign(&self, message: &[u8]) -> Result<Vec<u8>, SignerError> {
        // rustls drives this on reqwest's internal blocking thread, which is NOT attached to
        // the JVM. Attach it; the guard auto-detaches when it drops at end of scope.
        let mut env = self
            .vm
            .attach_current_thread()
            .map_err(|e| SignerError::new(format!("JNI attach_current_thread failed: {e}")))?;

        let jmsg = env
            .byte_array_from_slice(message)
            .map_err(|e| SignerError::new(format!("JNI byte_array_from_slice failed: {e}")))?;

        let result = env.call_method(
            self.signer.as_obj(),
            "sign",
            "([B)[B",
            &[JValue::Object(&jmsg)],
        );

        // Surface + clear any Java-side exception before inspecting the result, so the thread
        // is never left with a pending exception when control returns to rustls.
        if env.exception_check().unwrap_or(false) {
            let _ = env.exception_clear();
            return Err(SignerError::new(
                "java client-auth signer threw (StrongBox sign failed or key unavailable)",
            ));
        }

        let obj = result
            .and_then(|v| v.l())
            .map_err(|e| SignerError::new(format!("JNI sign call failed: {e}")))?;
        if obj.is_null() {
            return Err(SignerError::new("java client-auth signer returned null"));
        }

        let arr = JByteArray::from(obj);
        env.convert_byte_array(&arr)
            .map_err(|e| SignerError::new(format!("JNI convert_byte_array failed: {e}")))
    }
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

/// `com.cadence.agent.core.CoreBridge.nativeInitWithSigner(String, ClientAuthSigner) -> long`
/// (0 on failure, detail via `nativeLastError()`). Like `nativeInit`, but the client private
/// key stays in Android StrongBox: the mTLS client-auth signature is produced by the Java
/// `signer` (a `com.cadence.agent.core.ClientAuthSigner`, see [`JavaSigner`]). The config JSON
/// is the [`crate::ffi`] signer-path shape — `cert_chain_pem` (client chain, leaf first),
/// **no** `client_identity_pem`.
///
/// Device/emulator validation pending (needs a real StrongBox key); compiled + reviewed here.
#[cfg(feature = "https")]
#[no_mangle]
pub extern "system" fn Java_com_cadence_agent_core_CoreBridge_nativeInitWithSigner<'local>(
    mut env: JNIEnv<'local>,
    _class: JClass<'local>,
    config_json: JString<'local>,
    signer: JObject<'local>,
) -> jlong {
    catch_unwind(AssertUnwindSafe(|| {
        let json = match read_jstring(&mut env, &config_json) {
            Ok(s) => s,
            Err(msg) => {
                jni_set_last_error(CadenceStatus::InvalidArg, msg);
                return 0;
            }
        };
        // Hold a JVM handle (to attach the signing thread later) and a global ref to the Java
        // signer (to outlive this call and cross threads).
        let vm = match env.get_java_vm() {
            Ok(vm) => vm,
            Err(e) => {
                jni_set_last_error(
                    CadenceStatus::Error,
                    format!("nativeInitWithSigner: get_java_vm failed: {e}"),
                );
                return 0;
            }
        };
        let global = match env.new_global_ref(&signer) {
            Ok(g) => g,
            Err(e) => {
                jni_set_last_error(
                    CadenceStatus::Error,
                    format!("nativeInitWithSigner: new_global_ref failed: {e}"),
                );
                return 0;
            }
        };
        let signer: std::sync::Arc<dyn ClientAuthSigner> =
            std::sync::Arc::new(JavaSigner { vm, signer: global });
        match init_with_signer_from_config_json(&json, signer) {
            Ok(handle) => std::boxed::Box::into_raw(handle) as usize as jlong,
            Err(msg) => {
                jni_set_last_error(CadenceStatus::Error, msg);
                0
            }
        }
    }))
    .unwrap_or_else(|_| {
        jni_set_last_error(CadenceStatus::Error, "nativeInitWithSigner: panic");
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
