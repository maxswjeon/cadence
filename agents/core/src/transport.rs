//! Transport abstraction: how an envelope reaches the brain's `POST /ingest/event`.
//!
//! The core is transport-agnostic. Production uses [`HttpsTransport`] (reqwest + rustls,
//! mutual TLS). Tests use [`MockTransport`], which reproduces the brain's dedupe semantics
//! and can inject `503`/network faults to prove no-loss / no-dup delivery.

use crate::envelope::EventEnvelope;

/// The brain's response to an ingest attempt, mapped from HTTP status.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Outcome {
    /// `202 Accepted` — a new event was durably ingested. Safe to ack + drop.
    Accepted {
        /// The dedupe id the brain recorded for this event.
        dedupe_id: String,
    },
    /// `200 OK` with `duplicate=true` — the brain already had this `dedupe_id`. Also safe
    /// to ack + drop (delivery is confirmed; this is the exactly-once collapse point).
    Duplicate {
        /// The dedupe id the brain already had.
        dedupe_id: String,
    },
    /// `503` — brain backpressure. Keep the event buffered and retry later; do **not** drop.
    Backpressure,
    /// `422` — the envelope violated the raw boundary (a permanent client error). Retrying
    /// is futile; the agent dead-letters it.
    Rejected {
        /// Why the brain rejected the envelope.
        reason: String,
    },
    /// Network drop / timeout / non-standard status. Transient: retry with backoff.
    NetworkError {
        /// Human-readable transport failure detail.
        detail: String,
    },
}

impl Outcome {
    /// Delivery is confirmed at the brain (new or duplicate) — the event may be acked.
    pub fn is_delivered(&self) -> bool {
        matches!(self, Outcome::Accepted { .. } | Outcome::Duplicate { .. })
    }
}

/// Sends a single envelope to the brain. Implementations must be idempotent-friendly:
/// sending the same envelope twice is expected (the brain dedupes on `dedupe_id`).
pub trait Transport {
    /// Attempt to deliver one envelope, returning the mapped [`Outcome`].
    fn send(&self, envelope: &EventEnvelope) -> Outcome;
}

// --------------------------------------------------------------------------- //
// MockTransport
// --------------------------------------------------------------------------- //

use std::collections::{HashSet, VecDeque};
use std::sync::Mutex;

/// A scripted fault to return **before** normal processing, one per `send` call.
#[derive(Debug, Clone, Copy)]
pub enum Fault {
    /// Simulate the request never reaching the brain.
    NetworkDrop,
    /// Simulate the brain returning `503` backpressure.
    Backpressure,
}

/// In-memory transport that mimics the brain: the first time it *processes* a given
/// `dedupe_id` it returns [`Outcome::Accepted`]; every later time [`Outcome::Duplicate`].
/// This is exactly the brain's `202`-then-`200` behavior, so a test can assert
/// exactly-once by checking each dedupe id was `Accepted` at most once.
#[derive(Default)]
pub struct MockTransport {
    state: Mutex<MockState>,
}

#[derive(Default)]
struct MockState {
    /// dedupe ids the brain has durably ingested.
    seen: HashSet<String>,
    /// dedupe ids in the order they first got a `202` — length == unique events delivered.
    accepted_log: Vec<String>,
    /// Faults to apply to the next `send` calls, consumed front-to-back.
    faults: VecDeque<Fault>,
    /// Total `send` calls observed (retries included).
    calls: usize,
}

impl MockTransport {
    /// A fault-free mock.
    pub fn new() -> Self {
        Self::default()
    }

    /// Queue faults applied to the next `send` calls (one each), before normal processing.
    pub fn push_faults(&self, faults: impl IntoIterator<Item = Fault>) {
        let mut s = self.state.lock().unwrap();
        s.faults.extend(faults);
    }

    /// Number of unique events the brain has accepted (fresh `202`s).
    pub fn accepted_count(&self) -> usize {
        self.state.lock().unwrap().accepted_log.len()
    }

    /// The dedupe ids that received a fresh `202`, in order.
    pub fn accepted_log(&self) -> Vec<String> {
        self.state.lock().unwrap().accepted_log.clone()
    }

    /// Total `send` calls, including retries.
    pub fn call_count(&self) -> usize {
        self.state.lock().unwrap().calls
    }

    /// Whether the brain has ingested this dedupe id.
    pub fn has_seen(&self, dedupe_id: &str) -> bool {
        self.state.lock().unwrap().seen.contains(dedupe_id)
    }
}

impl Transport for MockTransport {
    fn send(&self, envelope: &EventEnvelope) -> Outcome {
        let mut s = self.state.lock().unwrap();
        s.calls += 1;

        if let Some(fault) = s.faults.pop_front() {
            return match fault {
                Fault::NetworkDrop => Outcome::NetworkError {
                    detail: "simulated network drop".into(),
                },
                Fault::Backpressure => Outcome::Backpressure,
            };
        }

        let id = envelope.dedupe_key();
        if s.seen.contains(&id) {
            Outcome::Duplicate { dedupe_id: id }
        } else {
            s.seen.insert(id.clone());
            s.accepted_log.push(id.clone());
            Outcome::Accepted { dedupe_id: id }
        }
    }
}

// --------------------------------------------------------------------------- //
// HttpsTransport (reqwest + rustls, mutual TLS)
// --------------------------------------------------------------------------- //

/// Real mTLS transport to the brain's ingest endpoint.
///
/// Uses a blocking reqwest client with rustls, presenting a **client certificate**
/// (mutual TLS) and pinning the brain's CA. Status mapping matches
/// `cadence/brain/app.py`: `202`→[`Outcome::Accepted`], `200`→[`Outcome::Duplicate`],
/// `503`→[`Outcome::Backpressure`], `422`→[`Outcome::Rejected`], anything else / transport
/// failure → [`Outcome::NetworkError`].
#[cfg(feature = "https")]
pub struct HttpsTransport {
    client: reqwest::blocking::Client,
    ingest_url: String,
}

#[cfg(feature = "https")]
impl HttpsTransport {
    /// Build an mTLS client.
    ///
    /// * `base_url` — brain origin, e.g. `https://brain.local:8443`.
    /// * `client_identity_pem` — the agent's **client cert + private key** concatenated in
    ///   one PEM bundle (presented for mutual TLS).
    /// * `ca_pem` — the CA certificate that signed the brain's server cert (pinned).
    pub fn new(
        base_url: &str,
        client_identity_pem: &[u8],
        ca_pem: &[u8],
    ) -> std::result::Result<Self, reqwest::Error> {
        let identity = reqwest::Identity::from_pem(client_identity_pem)?;
        let ca = reqwest::Certificate::from_pem(ca_pem)?;
        let client = reqwest::blocking::Client::builder()
            .use_rustls_tls()
            .identity(identity)
            .add_root_certificate(ca)
            // Enforce mTLS + pinned CA only; no plaintext fallback.
            .https_only(true)
            .build()?;
        Ok(HttpsTransport {
            client,
            ingest_url: format!("{}/ingest/event", base_url.trim_end_matches('/')),
        })
    }
}

#[cfg(feature = "https")]
impl Transport for HttpsTransport {
    fn send(&self, envelope: &EventEnvelope) -> Outcome {
        let fallback_id = envelope.dedupe_key();
        let resp = match self.client.post(&self.ingest_url).json(envelope).send() {
            Ok(r) => r,
            Err(e) => {
                return Outcome::NetworkError {
                    detail: e.to_string(),
                }
            }
        };
        let status = resp.status().as_u16();
        // The brain echoes `dedupe_id`; fall back to the locally-computed one if parsing fails.
        let dedupe_id = resp
            .json::<serde_json::Value>()
            .ok()
            .and_then(|b| {
                b.get("dedupe_id")
                    .and_then(|v| v.as_str().map(str::to_string))
            })
            .unwrap_or(fallback_id);

        match status {
            202 => Outcome::Accepted { dedupe_id },
            200 => Outcome::Duplicate { dedupe_id },
            503 => Outcome::Backpressure,
            422 => Outcome::Rejected {
                reason: "raw_boundary_violation".into(),
            },
            other => Outcome::NetworkError {
                detail: format!("unexpected status {other}"),
            },
        }
    }
}
