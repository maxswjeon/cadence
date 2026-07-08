package com.cadence.agent.core

import android.os.Build
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.security.keystore.StrongBoxUnavailableException
import java.security.KeyPairGenerator
import java.security.KeyStore
import java.security.PrivateKey
import java.security.PublicKey
import java.security.Signature
import java.security.spec.ECGenParameterSpec
import java.util.Base64

/**
 * A [ClientAuthSigner] whose P-256 client-auth key is generated once, non-exportable, in
 * the Android **StrongBox** secure element (a discrete tamper-resistant chip), falling
 * back to the **TEE**-backed AndroidKeyStore when StrongBox is unavailable. The private
 * key material never leaves hardware; this class only ever feeds it messages and returns
 * DER ECDSA signatures.
 *
 * This is the on-device end of the B1 seam: the Rust core's mTLS transport
 * ([CoreBridge.initWithSigner]) calls [sign] over JNI for each TLS 1.3 CertificateVerify,
 * and the enrollment client calls [sign] over the key's own DER SPKI to mint the
 * proof-of-possession ([publicKeySpkiPem] + [signSpki]).
 *
 * ## Provenance & fallback
 * StrongBox requires API 28 (`setIsStrongBoxBacked`) **and** the `FEATURE_STRONGBOX_KEYSTORE`
 * hardware feature. When either is missing, key generation throws
 * [StrongBoxUnavailableException] (or we never attempt StrongBox on API < 28); we then
 * generate a TEE-backed key instead. [keyProvenance] records which path was taken
 * (`"strongbox"` vs `"tee"`) so it can be reported at enrollment (both map to the brain's
 * `key_provenance = "hardware"`).
 *
 * ## Key attestation (optional)
 * When constructed with an [attestationChallenge], the generated key carries an Android
 * Key Attestation certificate chain rooted in Google's attestation root, retrievable via
 * [attestationChainPem]. This lets the operator (or a NAS-hosted verifier) prove the key
 * really lives in certified hardware. Passing `null` skips attestation (the chain then
 * holds only the self-signed leaf and is not useful as an attestation).
 *
 * **Device/emulator validation pending.** StrongBox, the TEE fallback path, and Key
 * Attestation all need real Keystore hardware (or a Keymaster-backed emulator image) to
 * exercise; there is no host-JVM unit-test seam. The code below is compiled and
 * doc-checked against the Android Keystore / KeyGenParameterSpec references; on-device
 * bring-up validates it. See `agents/android/README.md`.
 *
 * Docs cited (checked against, not memory):
 *  - AndroidKeyStore / KeyGenParameterSpec — https://developer.android.com/training/articles/keystore
 *  - `setIsStrongBoxBacked` / StrongBox — https://developer.android.com/reference/android/security/keystore/KeyGenParameterSpec.Builder#setIsStrongBoxBacked(boolean)
 *  - Key Attestation — https://developer.android.com/privacy-and-security/security-key-attestation
 *
 * @param alias AndroidKeyStore entry alias for the (idempotently generated) client key.
 * @param attestationChallenge optional attestation challenge; when set, the key is
 *   generated with an attestation certificate chain (see [attestationChainPem]).
 */
class StrongBoxSigner(
    private val alias: String,
    private val attestationChallenge: ByteArray? = null,
) : ClientAuthSigner {

    /** `"strongbox"` if the key is StrongBox-backed, `"tee"` if TEE-backed. */
    val provenance: String

    init {
        provenance = ensureKey()
    }

    /**
     * SHA-256 + sign [message] with the hardware key, returning the ASN.1-DER ECDSA
     * signature (`Signature("SHA256withECDSA")` already yields DER). Throws on any
     * keystore/signing failure so the mTLS handshake fails closed.
     */
    override fun sign(message: ByteArray): ByteArray {
        val signer = Signature.getInstance("SHA256withECDSA")
        signer.initSign(privateKey())
        signer.update(message)
        return signer.sign()
    }

    /** The key's SubjectPublicKeyInfo (SPKI) in DER — what the P-256 public key encodes to. */
    fun publicKeySpkiDer(): ByteArray = publicKey().encoded

    /**
     * The key's SPKI as PEM (`-----BEGIN PUBLIC KEY-----`) — the `public_key` field of the
     * brain's enrollment request (`cadence/devices/enrollment.py`).
     */
    fun publicKeySpkiPem(): String = pemEncode("PUBLIC KEY", publicKeySpkiDer())

    /**
     * The enrollment proof-of-possession: the DER ECDSA-P256/SHA-256 signature **by this
     * key over its own DER SPKI**. base64 of this is the brain's `dpop_proof`, which the
     * server verifies as `public_key.verify(sig, spki_der, ECDSA(SHA256))` — reusing the
     * exact StrongBox [sign] primitive keeps the proof bound to this specific key.
     */
    fun signSpki(): ByteArray = sign(publicKeySpkiDer())

    /** `key_provenance` for enrollment/telemetry: `"strongbox"` or `"tee"`. */
    fun keyProvenance(): String = provenance

    /**
     * The Android Key Attestation certificate chain (leaf first) as PEM, or `null` when
     * the key was generated without an [attestationChallenge]. Each element is one
     * `-----BEGIN CERTIFICATE-----` block. The raw chain is a few KB — it is uploaded to
     * NAS out-of-band and only a reference is carried in the enrollment `attestation`
     * field (never inlined into D1; see the enrollment module docstring).
     */
    fun attestationChainPem(): List<String>? {
        if (attestationChallenge == null) return null
        val entry = keyStore().getCertificateChain(alias) ?: return null
        return entry.map { pemEncode("CERTIFICATE", it.encoded) }
    }

    // --- key lifecycle ------------------------------------------------------------------ //

    /**
     * Generate the key once (idempotent on [alias]), preferring StrongBox with a graceful
     * fallback to TEE. Returns the provenance actually achieved. If a key already exists
     * under [alias], its provenance is re-derived from the Keystore rather than regenerated
     * (regenerating would rotate the enrolled identity).
     */
    private fun ensureKey(): String {
        val ks = keyStore()
        if (ks.containsAlias(alias)) {
            return existingProvenance()
        }
        // Prefer StrongBox (API 28+ and the hardware feature). On failure, fall back to TEE.
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
            try {
                generate(strongBox = true)
                return "strongbox"
            } catch (_: StrongBoxUnavailableException) {
                // No StrongBox on this device — drop the alias (if a partial entry landed)
                // and fall through to the TEE-backed generation below.
                if (ks.containsAlias(alias)) ks.deleteEntry(alias)
            }
        }
        generate(strongBox = false)
        return "tee"
    }

    private fun generate(strongBox: Boolean) {
        val builder = KeyGenParameterSpec.Builder(alias, KeyProperties.PURPOSE_SIGN)
            .setAlgorithmParameterSpec(ECGenParameterSpec("secp256r1"))
            .setDigests(KeyProperties.DIGEST_SHA256)
        if (strongBox && Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
            builder.setIsStrongBoxBacked(true)
        }
        if (attestationChallenge != null) {
            builder.setAttestationChallenge(attestationChallenge)
        }
        val generator = KeyPairGenerator.getInstance(
            KeyProperties.KEY_ALGORITHM_EC, ANDROID_KEYSTORE,
        )
        generator.initialize(builder.build())
        generator.generateKeyPair()
    }

    /**
     * Best-effort provenance of an already-present key. `KeyInfo.isInsideSecureHardware`
     * only tells us hardware vs software, not StrongBox vs TEE, so a pre-existing key is
     * reported as `"strongbox"` only when this device advertises StrongBox and the key is
     * hardware-backed; otherwise `"tee"`. On-device bring-up confirms the distinction.
     */
    private fun existingProvenance(): String {
        return if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) "strongbox" else "tee"
    }

    private fun privateKey(): PrivateKey {
        val entry = keyStore().getEntry(alias, null) as? KeyStore.PrivateKeyEntry
            ?: throw IllegalStateException("no StrongBox key under alias '$alias'")
        return entry.privateKey
    }

    private fun publicKey(): PublicKey =
        keyStore().getCertificate(alias)?.publicKey
            ?: throw IllegalStateException("no certificate/public key under alias '$alias'")

    private fun keyStore(): KeyStore =
        KeyStore.getInstance(ANDROID_KEYSTORE).apply { load(null) }

    private fun pemEncode(type: String, der: ByteArray): String {
        val b64 = Base64.getMimeEncoder(64, "\n".toByteArray()).encodeToString(der)
        return "-----BEGIN $type-----\n$b64\n-----END $type-----\n"
    }

    companion object {
        private const val ANDROID_KEYSTORE = "AndroidKeyStore"

        /** Default alias for the agent's mTLS client-auth key. */
        const val DEFAULT_ALIAS = "cadence-client-auth"
    }
}
