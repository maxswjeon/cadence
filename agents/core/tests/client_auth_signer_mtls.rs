//! Integration test for the **real** delegated-signer client-auth transport.
//!
//! Where `delegated_signer_mtls.rs` was the M8 feasibility spike (hand-rolled rustls glue
//! proving the mechanism), this exercises the promoted production API on
//! [`cadence_agent_core::HttpsTransport`]:
//!
//!   * [`HttpsTransport::with_client_auth_signer`] builds the mTLS reqwest client with the
//!     client private key held **only** behind a [`ClientAuthSigner`] — never as exportable
//!     PEM to rustls/reqwest.
//!   * [`SoftwareSigner`] is the in-memory `ring` fallback used here (and the documented
//!     software impl); a thin counting wrapper proves the seam was actually invoked for the
//!     TLS CertificateVerify signature.
//!
//! End to end over loopback, no external network: mint an in-memory EC P-256 CA + server
//! cert + client cert, stand up a pure-std rustls server that REQUIRES client auth, then
//! drive a real `Transport::send` and assert the brain-shaped `202` maps to
//! [`Outcome::Accepted`] AND the delegated signer was called.
//!
//! Compiles only with the default `https` feature (the transport lives behind it).
#![cfg(feature = "https")]

use std::io::{Read as _, Write as _};
use std::net::{IpAddr, Ipv4Addr, TcpListener};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;
use std::thread;

use cadence_agent_core::{
    ClientAuthSigner, EventEnvelope, HttpsTransport, Outcome, SignerError, SoftwareSigner,
    Transport,
};

use rcgen::{
    BasicConstraints, CertificateParams, ExtendedKeyUsagePurpose, IsCa, Issuer, KeyPair,
    KeyUsagePurpose, SanType, PKCS_ECDSA_P256_SHA256,
};
use rustls::pki_types::{CertificateDer, PrivateKeyDer, PrivatePkcs8KeyDer};
use rustls::server::WebPkiClientVerifier;
use rustls::{RootCertStore, ServerConfig, ServerConnection, Stream};

/// A [`ClientAuthSigner`] that forwards to a [`SoftwareSigner`] and counts how many times
/// the seam was asked to sign — the load-bearing evidence that the client private key was
/// exercised only through the delegated signer during the handshake.
struct CountingSigner {
    inner: SoftwareSigner,
    calls: Arc<AtomicUsize>,
}

impl ClientAuthSigner for CountingSigner {
    fn sign(&self, message: &[u8]) -> Result<Vec<u8>, SignerError> {
        self.calls.fetch_add(1, Ordering::SeqCst);
        self.inner.sign(message)
    }
}

/// A minted cert: its DER (for the server's root store / config), plus PEM cert and PEM
/// PKCS#8 key (what the production transport API consumes).
struct Minted {
    cert_der: CertificateDer<'static>,
    cert_pem: String,
    key: KeyPair,
}

/// Mint a self-signed EC P-256 CA; return its issuer handle, cert DER, and cert PEM.
fn make_ca() -> (Issuer<'static, KeyPair>, CertificateDer<'static>, String) {
    let mut params = CertificateParams::new(Vec::new()).expect("ca params");
    params.is_ca = IsCa::Ca(BasicConstraints::Unconstrained);
    params.key_usages = vec![
        KeyUsagePurpose::KeyCertSign,
        KeyUsagePurpose::CrlSign,
        KeyUsagePurpose::DigitalSignature,
    ];
    let key = KeyPair::generate_for(&PKCS_ECDSA_P256_SHA256).expect("ca key");
    let cert = params.self_signed(&key).expect("ca self-sign");
    let cert_der = cert.der().clone();
    let cert_pem = cert.pem();
    (Issuer::new(params, key), cert_der, cert_pem)
}

/// Mint a leaf cert (server or client) signed by `issuer`.
fn make_leaf(
    issuer: &Issuer<'static, KeyPair>,
    san: SanType,
    eku: ExtendedKeyUsagePurpose,
) -> Minted {
    let mut params = CertificateParams::new(Vec::new()).expect("leaf params");
    params.subject_alt_names.push(san);
    params.extended_key_usages.push(eku);
    let key = KeyPair::generate_for(&PKCS_ECDSA_P256_SHA256).expect("leaf key");
    let cert = params.signed_by(&key, issuer).expect("leaf sign");
    Minted {
        cert_der: cert.der().clone(),
        cert_pem: cert.pem(),
        key,
    }
}

#[test]
fn with_client_auth_signer_completes_mtls_and_delivers() {
    let provider = Arc::new(rustls::crypto::ring::default_provider());

    // --- In-memory PKI: CA -> server cert (IP SAN) + client cert (clientAuth EKU) --------
    let (issuer, ca_der, ca_pem) = make_ca();
    let server = make_leaf(
        &issuer,
        SanType::IpAddress(IpAddr::V4(Ipv4Addr::LOCALHOST)),
        ExtendedKeyUsagePurpose::ServerAuth,
    );
    let client = make_leaf(
        &issuer,
        SanType::IpAddress(IpAddr::V4(Ipv4Addr::LOCALHOST)),
        ExtendedKeyUsagePurpose::ClientAuth,
    );

    // --- Server side: pure-std rustls server that REQUIRES client auth against the CA ----
    let mut roots = RootCertStore::empty();
    roots.add(ca_der).expect("add ca root");
    let roots = Arc::new(roots);

    let verifier = WebPkiClientVerifier::builder_with_provider(roots.clone(), provider.clone())
        .build()
        .expect("client verifier");
    let server_key = PrivateKeyDer::Pkcs8(PrivatePkcs8KeyDer::from(server.key.serialize_der()));
    let server_config = ServerConfig::builder_with_provider(provider.clone())
        .with_safe_default_protocol_versions()
        .expect("server protocol versions")
        .with_client_cert_verifier(verifier)
        .with_single_cert(vec![server.cert_der.clone()], server_key)
        .expect("server single cert");
    let server_config = Arc::new(server_config);

    let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).expect("bind");
    let port = listener.local_addr().expect("addr").port();

    // One connection: require the client cert, then reply with the brain's 202 shape
    // (`{"dedupe_id": ...}`). Returns the number of client certs the server authenticated.
    let server_thread = thread::spawn(move || -> usize {
        let (mut sock, _peer) = listener.accept().expect("accept");
        let mut conn = ServerConnection::new(server_config).expect("server conn");
        let mut tls = Stream::new(&mut conn, &mut sock);

        // Drain the request headers (drives the handshake to completion).
        let mut acc: Vec<u8> = Vec::new();
        let mut buf = [0u8; 512];
        loop {
            match tls.read(&mut buf) {
                Ok(0) => break,
                Ok(n) => {
                    acc.extend_from_slice(&buf[..n]);
                    if acc.windows(4).any(|w| w == b"\r\n\r\n") {
                        break;
                    }
                }
                Err(_) => break,
            }
        }

        let peer_certs = tls.conn.peer_certificates().map(<[_]>::len).unwrap_or(0);

        let body = br#"{"dedupe_id":"server-assigned-dedupe"}"#;
        let head = format!(
            "HTTP/1.1 202 Accepted\r\ncontent-type: application/json\r\ncontent-length: {}\r\nconnection: close\r\n\r\n",
            body.len()
        );
        tls.write_all(head.as_bytes()).expect("write head");
        tls.write_all(body).expect("write body");
        tls.conn.send_close_notify();
        let _ = tls.flush();

        peer_certs
    });

    // --- Client side: the REAL production transport over a delegated SoftwareSigner ------
    // The client private key is loaded into `SoftwareSigner` from PEM and then only ever
    // reachable through the `ClientAuthSigner` trait object — reqwest/rustls never get it.
    let calls = Arc::new(AtomicUsize::new(0));
    let inner = SoftwareSigner::from_pkcs8_pem(client.key.serialize_pem().as_bytes())
        .expect("load software signer from client key PEM");
    let signer: Arc<dyn ClientAuthSigner> = Arc::new(CountingSigner {
        inner,
        calls: calls.clone(),
    });

    let transport = HttpsTransport::with_client_auth_signer(
        &format!("https://127.0.0.1:{port}"),
        client.cert_pem.as_bytes(),
        signer,
        ca_pem.as_bytes(),
    )
    .expect("build delegated-signer transport");

    let envelope = EventEnvelope::builder("evt-1", "kakaotalk", "acct-1", "message.posted")
        .payload_hash("hash-evt-1")
        .build();

    let outcome = transport.send(&envelope);

    assert_eq!(
        outcome,
        Outcome::Accepted {
            dedupe_id: "server-assigned-dedupe".to_string(),
        },
        "the 202 from the mTLS handshake must map to Accepted with the server's dedupe_id"
    );
    assert!(outcome.is_delivered());

    let peer_certs = server_thread.join().expect("server thread");
    assert_eq!(
        peer_certs, 1,
        "server must have authenticated exactly one client cert"
    );

    // The load-bearing assertion: the client-auth signature came through the delegated
    // signer, i.e. the private key was used only via the `ClientAuthSigner` seam.
    assert!(
        calls.load(Ordering::SeqCst) >= 1,
        "the delegated client-auth signer must have been invoked during the handshake"
    );
}

/// A malformed client cert chain PEM is rejected at construction (not a panic / silent
/// build of a broken client).
#[test]
fn with_client_auth_signer_rejects_bad_cert_pem() {
    let signer: Arc<dyn ClientAuthSigner> = {
        // A throwaway valid key so only the cert PEM is the failure under test.
        let key = KeyPair::generate_for(&PKCS_ECDSA_P256_SHA256).expect("key");
        let inner = SoftwareSigner::from_pkcs8_pem(key.serialize_pem().as_bytes()).expect("signer");
        Arc::new(inner)
    };

    let err = HttpsTransport::with_client_auth_signer(
        "https://127.0.0.1:1",
        b"-----BEGIN CERTIFICATE-----\nnot base64\n-----END CERTIFICATE-----\n",
        signer,
        b"-----BEGIN CERTIFICATE-----\nalso bad\n-----END CERTIFICATE-----\n",
    );
    assert!(err.is_err(), "malformed cert PEM must fail construction");
}
