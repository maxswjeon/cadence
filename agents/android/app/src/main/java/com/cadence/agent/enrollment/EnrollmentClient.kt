package com.cadence.agent.enrollment

import com.cadence.agent.core.StrongBoxSigner
import kotlinx.serialization.ExperimentalSerializationApi
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL
import java.util.Base64

/**
 * Client for the brain's un-authenticated device-enrollment intake
 * (`POST /device/enroll`, `cadence/devices/enrollment.py`) — the one endpoint reachable
 * **without** a client cert, because an un-enrolled device has none yet (the mTLS
 * chicken-and-egg boundary).
 *
 * ## Flow (Trust-On-First-Use + explicit human accept)
 * 1. This client builds an [EnrollmentRequest] from a [StrongBoxSigner]: the key's SPKI
 *    PEM as `public_key`, `key_provenance = "hardware"` (StrongBox/TEE both map here), and
 *    a `dpop_proof` = base64 of the signer's DER ECDSA signature over its **own DER SPKI**
 *    (proof-of-possession — anti-squatting; the brain verifies it against `public_key`).
 * 2. It POSTs the request. The brain records a single **`pending`** device row and returns
 *    `201` with `{status: "pending", device_id, public_key_fingerprint, ...}`. No cert is
 *    issued and no trust is conferred by enrolling — [EnrollmentResult.Pending].
 * 3. **Out of band**, a human operator reviews the pending device and runs
 *    `cadence device accept <device_id>` (the operator CLI + cert issuance are B2). The
 *    issued client certificate chain (leaf first) and the brain's CA pin are delivered
 *    back to the device out-of-band.
 * 4. The agent then brings up the mTLS transport with the StrongBox key still behind the
 *    seam: `CoreBridge.initWithSigner(configJson, strongBoxSigner)`, where `configJson`
 *    carries the issued `cert_chain_pem` + `ca_pem` (see [CoreBridge] and
 *    `agents/core/src/ffi.rs::SignerCoreConfig`).
 *
 * ## Transport trust
 * Enrollment runs before the device trusts the brain's CA via the core, so the brain's
 * server cert must still be validated here. Pass the brain CA PEM as [caPem] to pin it;
 * with `null`, the platform trust store is used (only appropriate when the brain presents
 * a publicly-trusted cert). Pinning is done by the caller-supplied [connectionFactory].
 *
 * **Device/network validation pending:** exercised on-device against a live brain; the
 * request/response shaping and PoP construction are compiled and doc-checked here.
 *
 * @param baseUrl brain base URL, e.g. `https://brain.example:8443`.
 * @param caPem optional brain CA PEM to pin (documentation of intent; wiring the pinned
 *   `SSLSocketFactory` is the caller's responsibility via [connectionFactory]).
 * @param connectionFactory opens a connection for a URL; override to inject a pinned
 *   `HttpsURLConnection` (default uses [URL.openConnection]).
 */
class EnrollmentClient(
    private val baseUrl: String,
    @Suppress("unused") private val caPem: String? = null,
    private val connectionFactory: (URL) -> HttpURLConnection = { url ->
        url.openConnection() as HttpURLConnection
    },
) {
    @OptIn(ExperimentalSerializationApi::class) // explicitNulls
    private val json = Json {
        // extra=forbid on the server: send exactly the fields we set, drop nulls
        // (csr/attestation stay absent) rather than emitting `null`s that would 422.
        explicitNulls = false
        encodeDefaults = true
        ignoreUnknownKeys = true
    }

    /**
     * Build and POST the enrollment request for [signer], labelling the device [deviceName].
     * An optional [attestationRef] (a NAS pointer to the uploaded Key Attestation chain,
     * NOT the raw blob) is carried through for the operator's B2 review.
     *
     * @throws EnrollmentException on a transport failure or a non-2xx/non-422/429 response.
     */
    fun enroll(
        signer: StrongBoxSigner,
        deviceName: String,
        attestationRef: String? = null,
    ): EnrollmentResult {
        val request = EnrollmentRequest(
            name = deviceName,
            publicKey = signer.publicKeySpkiPem(),
            // StrongBox and TEE are both hardware-resident; the brain's key_provenance
            // vocabulary is hardware|software|unknown. The fine-grained strongbox/tee
            // value lives in signer.keyProvenance() for local telemetry.
            keyProvenance = "hardware",
            dpopProof = Base64.getEncoder().encodeToString(signer.signSpki()),
            attestation = attestationRef,
        )
        return post(request)
    }

    private fun post(request: EnrollmentRequest): EnrollmentResult {
        val url = URL(baseUrl.trimEnd('/') + "/device/enroll")
        val body = json.encodeToString(EnrollmentRequest.serializer(), request)
            .toByteArray(Charsets.UTF_8)
        val conn = connectionFactory(url).apply {
            requestMethod = "POST"
            doOutput = true
            setRequestProperty("Content-Type", "application/json")
            setRequestProperty("Accept", "application/json")
            connectTimeout = CONNECT_TIMEOUT_MS
            readTimeout = READ_TIMEOUT_MS
        }
        try {
            conn.outputStream.use { it.write(body) }
            val code = conn.responseCode
            val stream = if (code in 200..299) conn.inputStream else conn.errorStream
            val text = stream?.readBytes()?.toString(Charsets.UTF_8) ?: ""
            return parse(code, text)
        } catch (e: IOException) {
            throw EnrollmentException("enrollment POST failed: ${e.message}", e)
        } finally {
            conn.disconnect()
        }
    }

    private fun parse(code: Int, text: String): EnrollmentResult {
        return when (code) {
            201, 200 -> {
                val resp = json.decodeFromString(EnrollmentResponse.serializer(), text)
                // The brain returns pending; never trusted/accepted on enroll.
                EnrollmentResult.Pending(
                    deviceId = resp.deviceId,
                    status = resp.status ?: "pending",
                    fingerprint = resp.publicKeyFingerprint,
                )
            }
            422 -> EnrollmentResult.Rejected(detail = errorDetail(text) ?: "invalid enrollment")
            429 -> EnrollmentResult.CapacityExceeded(detail = errorDetail(text) ?: "pending capacity")
            else -> throw EnrollmentException("unexpected enrollment status $code: $text")
        }
    }

    private fun errorDetail(text: String): String? =
        runCatching { json.decodeFromString(EnrollmentResponse.serializer(), text).detail }
            .getOrNull()

    companion object {
        private const val CONNECT_TIMEOUT_MS = 15_000
        private const val READ_TIMEOUT_MS = 15_000
    }
}

/**
 * The enrollment payload, field-for-field aligned with
 * `cadence.devices.enrollment.EnrollmentRequest` (`model_config = extra: forbid`). Only
 * `name`, `public_key`, `key_provenance`, and `dpop_proof` are always sent; `csr` is
 * never sent (we prove possession via DPoP, not a CSR) and `attestation` is sent only when
 * a NAS reference is supplied. Nulls are dropped by the client's `explicitNulls = false`.
 */
@Serializable
data class EnrollmentRequest(
    val name: String,
    @SerialName("public_key") val publicKey: String,
    @SerialName("key_provenance") val keyProvenance: String,
    @SerialName("dpop_proof") val dpopProof: String,
    val attestation: String? = null,
)

/** The brain's `/device/enroll` response body (`cadence/brain/app.py`). */
@Serializable
data class EnrollmentResponse(
    @SerialName("device_id") val deviceId: String? = null,
    val status: String? = null,
    @SerialName("public_key_fingerprint") val publicKeyFingerprint: String? = null,
    val accepted: Boolean = false,
    val trusted: Boolean = false,
    // Present on 422/429 error bodies.
    val error: String? = null,
    val detail: String? = null,
)

/** Outcome of an enrollment attempt. */
sealed class EnrollmentResult {
    /**
     * The brain recorded a `pending` device. No cert yet — an operator must run
     * `cadence device accept` and deliver the client cert + CA pin out-of-band, after
     * which the agent calls `CoreBridge.initWithSigner(...)`.
     */
    data class Pending(
        val deviceId: String?,
        val status: String,
        val fingerprint: String?,
    ) : EnrollmentResult()

    /** The brain rejected the payload (bad key / bad PoP) — HTTP 422. */
    data class Rejected(val detail: String) : EnrollmentResult()

    /** The anonymous pending-device cap is full — HTTP 429; retry later. */
    data class CapacityExceeded(val detail: String) : EnrollmentResult()
}

/** A transport-level or unexpected-status enrollment failure. */
class EnrollmentException(message: String, cause: Throwable? = null) :
    RuntimeException(message, cause)
