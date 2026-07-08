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

/// Delegating impl so a boxed (including `dyn`) transport is itself a [`Transport`].
///
/// This is what lets the FFI layer hold an `AgentCore<Box<dyn Transport>>` — a single
/// concrete `AgentCore` type whose transport is chosen at runtime (real [`HttpsTransport`]
/// in production, [`MockTransport`] when the FFI unit tests inject one). The bound is
/// `?Sized` so `Box<dyn Transport>` qualifies; the trait itself stays object-safe (`send`
/// takes `&self` and uses no generics), so `dyn Transport` remains a valid type.
impl<T: Transport + ?Sized> Transport for Box<T> {
    fn send(&self, envelope: &EventEnvelope) -> Outcome {
        (**self).send(envelope)
    }
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

// --------------------------------------------------------------------------- //
// Delegated-signer client auth (hardware-keystore seam)
// --------------------------------------------------------------------------- //
//
// The plain `HttpsTransport::new` path requires the client private key as exportable
// PEM. That is a non-starter on hardened devices, where the key lives in Android
// StrongBox / Windows CNG (TPM) / a PKCS#11 token and can *sign* but never *export*.
//
// The seam below lets the private key stay behind an external `sign` hook. rustls asks
// our custom [`ResolvesClientCert`] for the client cert chain plus a `SigningKey`; the
// `SigningKey`'s `Signer` forwards the exact CertificateVerify transcript bytes rustls
// wants signed to a [`ClientAuthSigner`], which hashes (SHA-256) and produces the
// ASN.1-DER ECDSA-P256 signature — exactly what a hardware keystore's `sign` returns.
// reqwest/rustls only ever hold trait objects downstream of the signer; there is no
// key-export path. [`SoftwareSigner`] is the in-memory `ring` fallback for
// devbox/CI/testing; platform agents supply a JNI/CNG/PKCS#11 impl over FFI.

#[cfg(feature = "https")]
use std::sync::Arc;

#[cfg(feature = "https")]
use ring::rand::SystemRandom;
#[cfg(feature = "https")]
use ring::signature::{EcdsaKeyPair, ECDSA_P256_SHA256_ASN1_SIGNING};
#[cfg(feature = "https")]
use rustls::client::ResolvesClientCert;
#[cfg(feature = "https")]
use rustls::pki_types::pem::PemObject;
#[cfg(feature = "https")]
use rustls::pki_types::{CertificateDer, PrivatePkcs8KeyDer};
#[cfg(feature = "https")]
use rustls::sign::{CertifiedKey, Signer, SigningKey};
#[cfg(feature = "https")]
use rustls::{ClientConfig, RootCertStore, SignatureAlgorithm, SignatureScheme};

/// The hardware-keystore seam for client-auth mTLS.
///
/// An implementer holds (or references) a P-256 private key it can **sign with but not
/// export** — Android StrongBox, Windows CNG/TPM, an Apple Secure Enclave, or a PKCS#11
/// token. Given the raw bytes rustls wants signed for the TLS CertificateVerify message,
/// it must hash them with SHA-256 and return the **ASN.1-DER-encoded ECDSA-P256**
/// signature (the `ECDSA_NISTP256_SHA256` scheme). [`SoftwareSigner`] is the reference
/// software impl; platform agents implement this trait over FFI against their keystore.
///
/// The trait is object-safe (`&self`, no generics) so it can be held as
/// `Arc<dyn ClientAuthSigner>`, and `Send + Sync` because rustls shares the resolved
/// signing key across the connection's threads.
#[cfg(feature = "https")]
pub trait ClientAuthSigner: Send + Sync {
    /// Sign `message` (the un-hashed CertificateVerify transcript bytes rustls supplies),
    /// returning an ASN.1-DER ECDSA-P256 signature over `SHA-256(message)`.
    fn sign(&self, message: &[u8]) -> std::result::Result<Vec<u8>, SignerError>;
}

/// Failure from a [`ClientAuthSigner`] — the hardware/software signer could not produce a
/// signature (keystore unavailable, user auth declined, key handle invalid, …).
#[cfg(feature = "https")]
#[derive(Debug)]
pub struct SignerError {
    detail: String,
}

#[cfg(feature = "https")]
impl SignerError {
    /// Wrap an arbitrary signer failure reason.
    pub fn new(detail: impl Into<String>) -> Self {
        Self {
            detail: detail.into(),
        }
    }
}

#[cfg(feature = "https")]
impl std::fmt::Display for SignerError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "client-auth signer failed: {}", self.detail)
    }
}

#[cfg(feature = "https")]
impl std::error::Error for SignerError {}

/// In-memory software [`ClientAuthSigner`]: holds a P-256 private key and signs with
/// `ring`. This is the fallback for devbox/CI/testing (and any deployment without a
/// hardware keystore); production hardened agents replace it with a keystore-backed impl.
///
/// The key material is owned here (unlike a hardware signer), but it is never handed to
/// rustls/reqwest — they only ever see the [`ClientAuthSigner`] trait object — so the
/// same no-export handshake path is exercised.
#[cfg(feature = "https")]
pub struct SoftwareSigner {
    key_pair: EcdsaKeyPair,
}

#[cfg(feature = "https")]
impl std::fmt::Debug for SoftwareSigner {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        // Opaque on purpose: never render key material.
        f.debug_struct("SoftwareSigner").finish_non_exhaustive()
    }
}

#[cfg(feature = "https")]
impl SoftwareSigner {
    /// Build from a PKCS#8-DER-encoded EC P-256 private key.
    pub fn from_pkcs8_der(pkcs8_der: &[u8]) -> std::result::Result<Self, ClientAuthError> {
        let rng = SystemRandom::new();
        let key_pair =
            EcdsaKeyPair::from_pkcs8(&ECDSA_P256_SHA256_ASN1_SIGNING, pkcs8_der, &rng)
                .map_err(|e| ClientAuthError::Key(format!("invalid PKCS#8 P-256 key: {e}")))?;
        Ok(Self { key_pair })
    }

    /// Build from a PEM-encoded PKCS#8 EC P-256 private key (`-----BEGIN PRIVATE KEY-----`).
    pub fn from_pkcs8_pem(pem: &[u8]) -> std::result::Result<Self, ClientAuthError> {
        let der = PrivatePkcs8KeyDer::from_pem_slice(pem)
            .map_err(|e| ClientAuthError::Key(format!("invalid PKCS#8 PEM: {e}")))?;
        Self::from_pkcs8_der(der.secret_pkcs8_der())
    }
}

#[cfg(feature = "https")]
impl ClientAuthSigner for SoftwareSigner {
    fn sign(&self, message: &[u8]) -> std::result::Result<Vec<u8>, SignerError> {
        // Fresh RNG per call (ring signs deterministically-ish with per-sig randomness);
        // this mirrors how a hardware keystore is invoked and keeps `SoftwareSigner: Sync`
        // without holding a shared RNG.
        let rng = SystemRandom::new();
        let sig = self
            .key_pair
            .sign(&rng, message)
            .map_err(|e| SignerError::new(format!("ring P-256 sign failed: {e}")))?;
        Ok(sig.as_ref().to_vec())
    }
}

/// A `rustls::sign::SigningKey` that owns no key bytes — it forwards to a
/// [`ClientAuthSigner`]. Only offers the ECDSA-P256-SHA256 scheme (what P-256 keystore
/// keys speak).
#[cfg(feature = "https")]
struct DelegatingSigningKey {
    signer: Arc<dyn ClientAuthSigner>,
}

#[cfg(feature = "https")]
impl std::fmt::Debug for DelegatingSigningKey {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("DelegatingSigningKey")
            .finish_non_exhaustive()
    }
}

#[cfg(feature = "https")]
impl SigningKey for DelegatingSigningKey {
    fn choose_scheme(&self, offered: &[SignatureScheme]) -> Option<Box<dyn Signer>> {
        offered
            .contains(&SignatureScheme::ECDSA_NISTP256_SHA256)
            .then(|| {
                Box::new(DelegatingSigner {
                    signer: self.signer.clone(),
                }) as Box<dyn Signer>
            })
    }

    fn algorithm(&self) -> SignatureAlgorithm {
        SignatureAlgorithm::ECDSA
    }
}

/// The per-handshake signer: forwards rustls's CertificateVerify bytes to the
/// [`ClientAuthSigner`].
#[cfg(feature = "https")]
struct DelegatingSigner {
    signer: Arc<dyn ClientAuthSigner>,
}

#[cfg(feature = "https")]
impl std::fmt::Debug for DelegatingSigner {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("DelegatingSigner").finish_non_exhaustive()
    }
}

#[cfg(feature = "https")]
impl Signer for DelegatingSigner {
    fn sign(&self, message: &[u8]) -> std::result::Result<Vec<u8>, rustls::Error> {
        self.signer
            .sign(message)
            .map_err(|e| rustls::Error::General(e.to_string()))
    }

    fn scheme(&self) -> SignatureScheme {
        SignatureScheme::ECDSA_NISTP256_SHA256
    }
}

/// The client-cert resolver rustls consults during the handshake: hands back the cert
/// chain plus the delegating signing key. No key bytes here either.
#[cfg(feature = "https")]
#[derive(Debug)]
struct DelegatingResolver {
    certified: Arc<CertifiedKey>,
}

#[cfg(feature = "https")]
impl ResolvesClientCert for DelegatingResolver {
    fn resolve(
        &self,
        _root_hint_subjects: &[&[u8]],
        _sigschemes: &[SignatureScheme],
    ) -> Option<Arc<CertifiedKey>> {
        Some(self.certified.clone())
    }

    fn has_certs(&self) -> bool {
        true
    }
}

/// Failure building an [`HttpsTransport`] over a [`ClientAuthSigner`]: bad PEM inputs,
/// an unusable key, a rejected rustls config, or a reqwest client build error.
#[cfg(feature = "https")]
#[derive(Debug)]
pub enum ClientAuthError {
    /// A PEM certificate (client chain or CA) could not be parsed.
    Pem(String),
    /// The client cert chain PEM contained no certificates.
    EmptyCertChain,
    /// The private key (for [`SoftwareSigner`]) was not a valid PKCS#8 P-256 key.
    Key(String),
    /// rustls rejected the assembled `ClientConfig` (e.g. bad root cert).
    Rustls(rustls::Error),
    /// reqwest failed to build the client from the preconfigured TLS config.
    Reqwest(reqwest::Error),
}

#[cfg(feature = "https")]
impl std::fmt::Display for ClientAuthError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            ClientAuthError::Pem(e) => write!(f, "invalid PEM certificate: {e}"),
            ClientAuthError::EmptyCertChain => write!(f, "client cert chain PEM was empty"),
            ClientAuthError::Key(e) => write!(f, "invalid client private key: {e}"),
            ClientAuthError::Rustls(e) => write!(f, "rustls config error: {e}"),
            ClientAuthError::Reqwest(e) => write!(f, "reqwest client build error: {e}"),
        }
    }
}

#[cfg(feature = "https")]
impl std::error::Error for ClientAuthError {}

#[cfg(feature = "https")]
impl From<rustls::Error> for ClientAuthError {
    fn from(e: rustls::Error) -> Self {
        ClientAuthError::Rustls(e)
    }
}

#[cfg(feature = "https")]
impl From<reqwest::Error> for ClientAuthError {
    fn from(e: reqwest::Error) -> Self {
        ClientAuthError::Reqwest(e)
    }
}

#[cfg(feature = "https")]
impl HttpsTransport {
    /// Build an mTLS client that presents a client certificate whose private key stays
    /// behind an external [`ClientAuthSigner`] (a hardware keystore, or [`SoftwareSigner`]).
    ///
    /// Unlike [`HttpsTransport::new`], the key is **never** supplied as exportable PEM: the
    /// client-auth signature is delegated to `signer` via a custom rustls
    /// `ResolvesClientCert`/`SigningKey`/`Signer` chain, and the finished `ClientConfig` is
    /// handed to reqwest with `use_preconfigured_tls`.
    ///
    /// * `base_url` — brain origin, e.g. `https://brain.local:8443`.
    /// * `cert_chain_pem` — the agent's client certificate chain, PEM (leaf first). The
    ///   matching private key lives inside `signer`, not here.
    /// * `signer` — the delegated signer that produces the client-auth signature.
    /// * `ca_pem` — the CA certificate(s) that signed the brain's server cert (pinned).
    pub fn with_client_auth_signer(
        base_url: &str,
        cert_chain_pem: &[u8],
        signer: Arc<dyn ClientAuthSigner>,
        ca_pem: &[u8],
    ) -> std::result::Result<Self, ClientAuthError> {
        // Pin the ring provider explicitly (matching reqwest's rustls-tls) so we never
        // depend on process-global default-provider install order, and so
        // `use_preconfigured_tls`'s exact-version downcast accepts the config.
        let provider = Arc::new(rustls::crypto::ring::default_provider());

        // Parse the client cert chain (leaf first), keeping the key out of the picture.
        let cert_chain: Vec<CertificateDer<'static>> =
            CertificateDer::pem_slice_iter(cert_chain_pem)
                .map(|r| r.map(CertificateDer::into_owned))
                .collect::<std::result::Result<_, _>>()
                .map_err(|e| ClientAuthError::Pem(e.to_string()))?;
        if cert_chain.is_empty() {
            return Err(ClientAuthError::EmptyCertChain);
        }

        // Pin the brain's CA(s).
        let mut roots = RootCertStore::empty();
        for ca in CertificateDer::pem_slice_iter(ca_pem) {
            let ca = ca.map_err(|e| ClientAuthError::Pem(e.to_string()))?;
            roots.add(ca)?;
        }

        // Wire the delegated signer into a resolver rustls will consult for client auth.
        let signing_key: Arc<dyn SigningKey> = Arc::new(DelegatingSigningKey { signer });
        let certified = Arc::new(CertifiedKey::new(cert_chain, signing_key));
        let resolver = Arc::new(DelegatingResolver { certified });

        let client_config = ClientConfig::builder_with_provider(provider)
            .with_safe_default_protocol_versions()?
            .with_root_certificates(roots)
            .with_client_cert_resolver(resolver);

        let client = reqwest::blocking::Client::builder()
            .use_preconfigured_tls(client_config)
            // Enforce mTLS + pinned CA only; no plaintext fallback.
            .https_only(true)
            .build()?;

        Ok(HttpsTransport {
            client,
            ingest_url: format!("{}/ingest/event", base_url.trim_end_matches('/')),
        })
    }
}
