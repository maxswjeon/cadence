//! `cadence-agent-core` — the portable, crash-safe capture core shared by Cadence's
//! device agents (Android via JNI, Windows via P/Invoke).
//!
//! It owns the security-critical, hard-to-get-right device logic **once**, in Rust:
//!
//! * [`EventEnvelope`] — the on-the-wire event, matching the brain's `Event`
//!   (`cadence/adapters/base.py`): structured fields + provenance pointers + a
//!   non-verbatim summary, **never** verbatim raw. Its `dedupe_id` is a content hash of
//!   stable fields so the brain can dedupe.
//! * [`wal::Wal`] — a crash-safe, append-only write-ahead log with a **bounded** pending
//!   set. Un-acked events survive a crash and are replayed; a torn tail is discarded. The
//!   bound gives **backpressure** (maps to the brain's `503`).
//! * [`Transport`] — how an envelope reaches `POST /ingest/event`; [`HttpsTransport`] is a
//!   reqwest+rustls **mutual-TLS** client, [`MockTransport`] a test double.
//! * [`AgentCore`] — ties them together with retry/backoff and drain-on-ack, giving
//!   **exactly-once** delivery (at-least-once on the wire, deduped at the brain).
//! * [`CaptureSource`] — read-only source trait; [`TimerSource`]/[`FakeSource`] are the
//!   reference impls (real OS capture belongs to the platform agents).
//!
//! See `README.md` for the FFI-binding intent and the full property list the tests prove.

// The crate is `unsafe`-free everywhere except the FFI boundary (`ffi.rs`, `jni.rs`), which
// must dereference raw pointers and export `#[no_mangle]` symbols. Rust's `forbid` level is
// deliberately un-overridable, so a crate-wide `#![forbid(unsafe_code)]` cannot grant those
// two modules a scoped exception (E0453). `deny` gives the identical guarantee — any
// unmarked `unsafe` anywhere is a hard compile error — while letting `ffi`/`jni` opt in via a
// module-level `#[allow(unsafe_code)]` with a `// SAFETY:` on every block.
#![deny(unsafe_code)]
#![warn(missing_docs)]

pub mod agent;
pub mod envelope;
pub mod error;
pub mod ffi;
#[cfg(feature = "jni")]
pub mod jni;
pub mod source;
pub mod transport;
pub mod wal;

pub use agent::{AgentCore, DrainReport, DrainStop, RetryPolicy};
pub use envelope::{AcquisitionTier, EventEnvelope, EventEnvelopeBuilder, SCHEMA_VERSION};
pub use error::{AgentError, Result};
pub use source::{CaptureSource, FakeSource, TimerSource};
pub use transport::{Fault, MockTransport, Outcome, Transport};
pub use wal::Wal;

#[cfg(feature = "https")]
pub use transport::{
    ClientAuthError, ClientAuthSigner, HttpsTransport, SignerError, SoftwareSigner,
};
