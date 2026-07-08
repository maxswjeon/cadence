//! Integration test for the **C-ABI** delegated-signer seam (`cadence_core_init_with_signer`).
//!
//! Where `client_auth_signer_mtls.rs` drives the Rust [`HttpsTransport`] API directly, this
//! goes one layer out and exercises the FFI the Windows P/Invoke agent binds to: it registers
//! an `extern "C"` [`CadenceSignCallback`] (the exact shape a hardware keystore's `sign` hook
//! satisfies), builds a live handle through [`cadence_core_init_with_signer`], then
//! captures + drains a real event so the mTLS handshake actually runs — proving the
//! FFI-callback seam produces a valid client-auth signature end to end.
//!
//! The callback here wraps an in-memory [`SoftwareSigner`] (a `ring` P-256 key) reached
//! through an opaque `ctx` pointer, standing in for the platform's secure element. A call
//! counter proves the client-auth signature came through the FFI callback, not any exportable
//! key handed to rustls/reqwest.
//!
//! End to end over loopback, no external network. Compiles only with the default `https`
//! feature (the transport lives behind it).
#![cfg(feature = "https")]

use std::ffi::{c_void, CStr, CString};
use std::io::{Read as _, Write as _};
use std::net::{IpAddr, Ipv4Addr, TcpListener};
use std::os::raw::c_char;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;
use std::thread;

use cadence_agent_core::ffi::{
    cadence_core_capture, cadence_core_drain, cadence_core_init_with_signer, cadence_core_shutdown,
    cadence_string_free, CadenceStatus,
};
use cadence_agent_core::{ClientAuthSigner, EventEnvelope, SoftwareSigner};

use rcgen::{
    BasicConstraints, CertificateParams, ExtendedKeyUsagePurpose, IsCa, Issuer, KeyPair,
    KeyUsagePurpose, SanType, PKCS_ECDSA_P256_SHA256,
};
use rustls::pki_types::{CertificateDer, PrivateKeyDer, PrivatePkcs8KeyDer};
use rustls::server::WebPkiClientVerifier;
use rustls::{RootCertStore, ServerConfig, ServerConnection, Stream};

/// The context the C callback reaches through the opaque `*mut c_void` — an in-memory
/// [`SoftwareSigner`] standing in for a hardware keystore, plus a counter proving the FFI
/// callback was the signing path.
struct CallbackCtx {
    signer: SoftwareSigner,
    calls: AtomicUsize,
}

/// An `extern "C"` [`cadence_agent_core::ffi::CadenceSignCallback`]: reconstruct the
/// [`CallbackCtx`] from `ctx`, SHA-256 + sign via the software P-256 key, and copy the DER
/// signature into the caller's buffer — exactly what a platform hardware-keystore hook does.
extern "C" fn test_sign_callback(
    ctx: *mut c_void,
    msg: *const u8,
    msg_len: usize,
    out_sig: *mut u8,
    out_sig_cap: usize,
    out_sig_len: *mut usize,
) -> i32 {
    // SAFETY: `ctx` is the `&CallbackCtx` we passed to `cadence_core_init_with_signer`, alive
    // for the whole test; `msg`/`msg_len` describe rustls's transcript slice; `out_sig` is a
    // writable buffer of `out_sig_cap` bytes and `out_sig_len` a writable `usize`.
    let ctx = unsafe { &*(ctx as *const CallbackCtx) };
    ctx.calls.fetch_add(1, Ordering::SeqCst);

    let message = unsafe { std::slice::from_raw_parts(msg, msg_len) };
    let sig = match ctx.signer.sign(message) {
        Ok(s) => s,
        Err(_) => return 1,
    };
    if sig.len() > out_sig_cap {
        return 2;
    }
    unsafe {
        std::ptr::copy_nonoverlapping(sig.as_ptr(), out_sig, sig.len());
        *out_sig_len = sig.len();
    }
    0
}

/// A minted cert: its DER (for the server's root store / config), plus PEM cert and the
/// key-pair (its PEM feeds the software signer standing in for the keystore).
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
fn init_with_signer_completes_mtls_and_delivers_through_c_abi() {
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

    // One connection: require the client cert, read the POST /ingest/event request, then reply
    // with the brain's 202 shape (`{"dedupe_id": ...}`). Returns how many client certs the
    // server authenticated.
    let server_thread = thread::spawn(move || -> usize {
        let (mut sock, _peer) = listener.accept().expect("accept");
        let mut conn = ServerConnection::new(server_config).expect("server conn");
        let mut tls = Stream::new(&mut conn, &mut sock);

        // Drain until the request headers end (drives the handshake + request to completion).
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

    // --- Client side: the REAL C ABI over an `extern "C"` sign callback ------------------
    // The client private key is loaded into a `SoftwareSigner` (stand-in for a keystore) and
    // reached only through the FFI callback + opaque ctx — never handed to rustls/reqwest.
    let dir = tempfile::tempdir().expect("tempdir");
    let wal_path = dir.path().join("callback-signer.wal");

    let ctx = Box::new(CallbackCtx {
        signer: SoftwareSigner::from_pkcs8_pem(client.key.serialize_pem().as_bytes())
            .expect("load software signer from client key PEM"),
        calls: AtomicUsize::new(0),
    });
    let ctx_ptr = &*ctx as *const CallbackCtx as *mut c_void;

    let config = serde_json::json!({
        "wal_path": wal_path.to_str().unwrap(),
        "capacity": 16,
        "base_url": format!("https://127.0.0.1:{port}"),
        "cert_chain_pem": client.cert_pem,
        "ca_pem": ca_pem,
    })
    .to_string();
    let config_c = CString::new(config).unwrap();

    let handle =
        cadence_core_init_with_signer(config_c.as_ptr(), Some(test_sign_callback), ctx_ptr);
    assert!(
        !handle.is_null(),
        "cadence_core_init_with_signer must build a live handle"
    );

    // Capture one event, then drain it — the drain is what actually performs the mTLS
    // handshake (and thus invokes the FFI sign callback) against the server.
    let envelope = EventEnvelope::builder("evt-1", "kakaotalk", "acct-1", "message.posted")
        .payload_hash("hash-evt-1")
        .build();
    let env_json = CString::new(serde_json::to_string(&envelope).unwrap()).unwrap();
    let mut out_id: *mut c_char = std::ptr::null_mut();
    let status = cadence_core_capture(handle, env_json.as_ptr(), &mut out_id);
    assert_eq!(
        status,
        CadenceStatus::Ok,
        "capture should append to the WAL"
    );
    assert!(!out_id.is_null());
    cadence_string_free(out_id);

    let report_ptr = cadence_core_drain(handle);
    assert!(
        !report_ptr.is_null(),
        "drain should succeed over the mTLS-with-callback-signer transport"
    );
    // SAFETY: non-null core-owned string from drain.
    let report_str = unsafe { CStr::from_ptr(report_ptr) }.to_str().unwrap();
    let report: serde_json::Value = serde_json::from_str(report_str).unwrap();
    assert_eq!(
        report["delivered"], 1,
        "the one captured event must have been delivered through the handshake"
    );
    assert_eq!(report["pending_after"], 0);
    assert_eq!(report["stop"], "drained");
    cadence_string_free(report_ptr);

    cadence_core_shutdown(handle);

    let peer_certs = server_thread.join().expect("server thread");
    assert_eq!(
        peer_certs, 1,
        "server must have authenticated exactly one client cert"
    );

    // The load-bearing assertion: the client-auth signature came through the FFI callback,
    // i.e. the key was used only via the `CadenceSignCallback` seam.
    assert!(
        ctx.calls.load(Ordering::SeqCst) >= 1,
        "the FFI sign callback must have been invoked during the handshake"
    );
}
