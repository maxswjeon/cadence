package com.cadence.agent.core

/**
 * The Kotlin side of the B1 hardware-keystore client-auth seam.
 *
 * The Rust core's mTLS transport ([CoreBridge.initWithSigner]) delegates each TLS
 * client-auth signature to an implementor of this interface instead of holding an
 * exportable private key. The canonical implementor is [StrongBoxSigner], whose P-256
 * private key lives in the Android **StrongBox** secure element (or the TEE-backed
 * Keystore as a fallback) and never crosses the JNI boundary — only the message goes in
 * and the ASN.1-DER ECDSA signature comes out.
 *
 * ## Contract (mirrors the Rust `JavaSigner` in `agents/core/src/jni.rs`)
 * The Rust `JavaSigner` resolves this object over JNI and invokes `sign([B)[B` on
 * rustls's (reqwest-internal) blocking handshake thread. The implementation MUST:
 *  - hash [message] with **SHA-256** and sign it with a **P-256 (secp256r1)** private key,
 *  - return the **ASN.1-DER-encoded ECDSA** signature — exactly what
 *    `java.security.Signature.getInstance("SHA256withECDSA")` produces over an EC
 *    `PrivateKey` (the `ecdsa_secp256r1_sha256` TLS 1.3 scheme),
 *  - **throw** on any signing failure (key unavailable, user auth declined, keystore
 *    error). A thrown exception makes the mTLS handshake fail closed; it is detected via
 *    `env.exception_check()` on the Rust side and surfaced as a `SignerError`.
 *
 * The same `SHA256withECDSA` primitive is reused, over the key's own DER SPKI, to mint
 * the enrollment proof-of-possession — see [StrongBoxSigner.sign] and the enrollment
 * client.
 */
interface ClientAuthSigner {
    /**
     * SHA-256 + sign [message] with the hardware P-256 key, returning the ASN.1-DER
     * ECDSA signature. Throws on any signing failure (fail-closed).
     */
    fun sign(message: ByteArray): ByteArray
}
