//! M8 feasibility spike: hardware-backed client-auth mTLS through the shared Rust core.
//!
//! GOAL (prove or disprove): the agent core can present a TLS **client certificate whose
//! private key is never available as exportable PEM/DER to reqwest or rustls**. The
//! client-auth handshake signature is delegated to an external signer — a stand-in for a
//! hardware keystore (Android StrongBox / Windows TPM / Apple Secure Enclave) — which
//! receives the bytes-to-sign and returns a signature. rustls/reqwest only ever hold a
//! `dyn SigningKey` trait object with no key-export method.
//!
//! What this test does, end to end, over loopback, with no external network:
//!   1. Mints an in-memory EC P-256 test CA + server cert + client cert with `rcgen`.
//!   2. Wraps the client private key behind a "hardware" closure (the delegate). The raw
//!      key material lives ONLY inside that closure; rustls gets a custom
//!      [`SigningKey`]/[`Signer`] that forwards `sign(message)` to it and records the call.
//!   3. Exposes that signer via a [`ResolvesClientCert`] in a `rustls::ClientConfig`, handed
//!      to reqwest 0.12 via `ClientBuilder::use_preconfigured_tls` — the exact production API.
//!   4. Stands up a pure-std blocking `rustls` server that REQUIRES client auth
//!      (`WebPkiClientVerifier` against the test CA).
//!   5. Performs a real mTLS handshake and ASSERTS: the request succeeds (HTTP 202), the
//!      server saw the client certificate, AND the delegate closure was actually invoked to
//!      produce the client-auth signature — i.e. the private key was used only via the
//!      external signer, never exported.
//!
//! The whole file compiles only with the default `https` feature (it needs reqwest's
//! blocking client); `cargo test --no-default-features` compiles it away.
#![cfg(feature = "https")]

use std::io::{Read, Write};
use std::net::{IpAddr, Ipv4Addr, TcpListener};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;
use std::thread;

use rcgen::{
    BasicConstraints, CertificateParams, ExtendedKeyUsagePurpose, IsCa, Issuer, KeyPair,
    KeyUsagePurpose, SanType, PKCS_ECDSA_P256_SHA256,
};
use ring::rand::SystemRandom;
use ring::signature::{EcdsaKeyPair, ECDSA_P256_SHA256_ASN1_SIGNING};
use rustls::client::ResolvesClientCert;
use rustls::pki_types::{CertificateDer, PrivateKeyDer, PrivatePkcs8KeyDer};
use rustls::server::WebPkiClientVerifier;
use rustls::sign::{CertifiedKey, Signer, SigningKey};
use rustls::{
    ClientConfig, RootCertStore, ServerConfig, ServerConnection, SignatureAlgorithm,
    SignatureScheme, Stream,
};

/// The "hardware" boundary. In production this closure is the ONLY thing that can touch the
/// private key (StrongBox/TPM/Secure Enclave `sign`); here it is a `ring` P-256 signer whose
/// key material never escapes. It takes the exact bytes rustls wants signed and returns the
/// ASN.1-DER ECDSA signature. Everything reqwest/rustls hold is downstream of this trait
/// object and has no way to export the key.
type HardwareSign = Arc<dyn Fn(&[u8]) -> Vec<u8> + Send + Sync>;

/// A `rustls::sign::SigningKey` that owns no key bytes — it forwards to [`HardwareSign`].
struct DelegatedP256Key {
    sign_fn: HardwareSign,
    /// How many times the delegate was asked to sign (the load-bearing evidence).
    calls: Arc<AtomicUsize>,
}

impl std::fmt::Debug for DelegatedP256Key {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        // Deliberately opaque: there is no key material to print.
        f.debug_struct("DelegatedP256Key").finish_non_exhaustive()
    }
}

impl SigningKey for DelegatedP256Key {
    fn choose_scheme(&self, offered: &[SignatureScheme]) -> Option<Box<dyn Signer>> {
        // StrongBox/TPM/Secure Enclave P-256 keys speak ECDSA-with-SHA256.
        offered
            .contains(&SignatureScheme::ECDSA_NISTP256_SHA256)
            .then(|| {
                Box::new(DelegatedP256Signer {
                    sign_fn: self.sign_fn.clone(),
                    calls: self.calls.clone(),
                }) as Box<dyn Signer>
            })
    }

    fn algorithm(&self) -> SignatureAlgorithm {
        SignatureAlgorithm::ECDSA
    }
}

/// The per-handshake signer. `sign` is where the delegation actually happens.
struct DelegatedP256Signer {
    sign_fn: HardwareSign,
    calls: Arc<AtomicUsize>,
}

impl std::fmt::Debug for DelegatedP256Signer {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("DelegatedP256Signer")
            .finish_non_exhaustive()
    }
}

impl Signer for DelegatedP256Signer {
    fn sign(&self, message: &[u8]) -> Result<Vec<u8>, rustls::Error> {
        // rustls passes the un-hashed transcript bytes; the delegate hashes (SHA-256) and
        // signs, exactly as a hardware keystore would. Record that hardware was invoked.
        self.calls.fetch_add(1, Ordering::SeqCst);
        Ok((self.sign_fn)(message))
    }

    fn scheme(&self) -> SignatureScheme {
        SignatureScheme::ECDSA_NISTP256_SHA256
    }
}

/// The client-cert resolver rustls asks during the handshake: hands back the cert chain +
/// our delegated signer. No key bytes here either.
#[derive(Debug)]
struct DelegatingResolver {
    certified: Arc<CertifiedKey>,
}

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

/// A minted cert + its owned key material (kept only where legitimately needed).
struct Minted {
    cert_der: CertificateDer<'static>,
    key: KeyPair,
}

/// Mint a self-signed EC P-256 CA and return its issuer handle + cert DER.
fn make_ca() -> (Issuer<'static, KeyPair>, CertificateDer<'static>) {
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
    (Issuer::new(params, key), cert_der)
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
        key,
    }
}

#[test]
fn delegated_signer_client_auth_mtls_handshake() {
    // Use the same ring provider reqwest's rustls-tls pulls, chosen explicitly so we never
    // depend on process-global default-provider install order.
    let provider = Arc::new(rustls::crypto::ring::default_provider());

    // --- 1. In-memory PKI: CA -> server cert (IP SAN) + client cert (clientAuth EKU) ------
    let (issuer, ca_der) = make_ca();
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

    let mut roots = RootCertStore::empty();
    roots.add(ca_der).expect("add ca root");
    let roots = Arc::new(roots);

    // --- 2. Wrap the CLIENT key behind the "hardware" closure ----------------------------
    // The raw PKCS#8 lives only inside the closure. `ring` performs the actual P-256
    // signature (SHA-256 + ASN.1-DER), which is byte-for-byte what a StrongBox/TPM/Secure
    // Enclave ECDSA `sign` must return. rustls/reqwest never receive these bytes.
    let calls = Arc::new(AtomicUsize::new(0));
    let sign_fn: HardwareSign = {
        let pkcs8 = client.key.serialize_der();
        Arc::new(move |message: &[u8]| {
            let rng = SystemRandom::new();
            let kp = EcdsaKeyPair::from_pkcs8(&ECDSA_P256_SHA256_ASN1_SIGNING, &pkcs8, &rng)
                .expect("load hardware key");
            kp.sign(&rng, message)
                .expect("hardware sign")
                .as_ref()
                .to_vec()
        })
    };

    let signing_key: Arc<dyn SigningKey> = Arc::new(DelegatedP256Key {
        sign_fn,
        calls: calls.clone(),
    });
    let certified = Arc::new(CertifiedKey::new(
        vec![client.cert_der.clone()],
        signing_key,
    ));
    let resolver = Arc::new(DelegatingResolver { certified });

    // --- 3. rustls ClientConfig using the delegated resolver, handed to reqwest ----------
    let client_config = ClientConfig::builder_with_provider(provider.clone())
        .with_safe_default_protocol_versions()
        .expect("client protocol versions")
        .with_root_certificates((*roots).clone())
        .with_client_cert_resolver(resolver);

    // --- 4. Local rustls server that REQUIRES client auth against the test CA ------------
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

    // Pure-std blocking rustls server thread: one connection, require client cert, reply 202.
    // Returns how many client certs it saw (proof the peer authenticated).
    let server_thread = thread::spawn(move || -> usize {
        let (mut sock, _peer) = listener.accept().expect("accept");
        let mut conn = ServerConnection::new(server_config).expect("server conn");
        let mut tls = Stream::new(&mut conn, &mut sock);

        // Read until the end of the request headers (drives the handshake to completion).
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

        // Handshake is done here — capture the authenticated client cert count.
        let peer_certs = tls.conn.peer_certificates().map(<[_]>::len).unwrap_or(0);

        let body = br#"{"dedupe_id":"m8-spike"}"#;
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

    // --- 5. reqwest performs the real mTLS request via the preconfigured rustls config ---
    let http = reqwest::blocking::Client::builder()
        .use_preconfigured_tls(client_config)
        .https_only(true)
        .build()
        .expect("reqwest client");

    let resp = http
        .post(format!("https://127.0.0.1:{port}/ingest/event"))
        .body(r#"{"probe":true}"#)
        .send()
        .expect("mTLS request should succeed");

    assert_eq!(
        resp.status().as_u16(),
        202,
        "server should accept the delegated-signer client-auth handshake"
    );

    let peer_certs = server_thread.join().expect("server thread");
    assert_eq!(
        peer_certs, 1,
        "server must have received exactly one client cert"
    );

    // The load-bearing assertion: the private key was exercised ONLY through the external
    // delegate. If this is >0, rustls produced the CertificateVerify signature by calling our
    // hardware stand-in — never by holding exportable key bytes.
    assert!(
        calls.load(Ordering::SeqCst) >= 1,
        "the delegated hardware signer must have been invoked for client auth"
    );
}
