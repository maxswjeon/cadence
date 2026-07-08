using System.Net;
using System.Net.Http;
using System.Net.Http.Json;
using System.Runtime.Versioning;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace Cadence.WindowsAgent.Security;

/// <summary>
/// Client for the brain's un-authenticated device-enrollment intake
/// (<c>POST /device/enroll</c>, <c>cadence/brain/app.py</c> → <c>cadence/devices/enrollment.py</c>).
///
/// <para>
/// This is the deliberate chicken-and-egg exception to the mTLS gate: an un-enrolled device has no
/// client certificate yet, so this endpoint is NOT behind mTLS and this client does NOT present a
/// client cert. It still MUST validate the brain's <b>server</b> certificate against the pinned
/// brain CA — configure that on the injected <see cref="HttpClient"/>'s handler (e.g. a
/// <c>SocketsHttpHandler</c> with <c>SslOptions.RemoteCertificateValidationCallback</c> pinned to
/// the CA), the same CA that <c>ca_pem</c> pins for the post-enrollment mTLS transport.
/// </para>
///
/// <para>
/// Proof of possession is required (a bare public key is rejected). We send a DPoP-style proof:
/// <c>dpop_proof = base64( DER ECDSA-P256/SHA-256 signature by the device key over the device's own
/// DER SPKI )</c>, produced by <see cref="TpmClientAuthSigner.SignToDer"/> over
/// <see cref="TpmClientAuthSigner.PublicKeySpkiDer"/>. The brain verifies it with
/// <c>public_key.verify(sig, spki_der, ec.ECDSA(SHA256))</c>.
/// </para>
/// </summary>
[SupportedOSPlatform("windows")]
internal sealed class EnrollmentClient
{
    private static readonly JsonSerializerOptions JsonOptions = new(JsonSerializerDefaults.Web);

    private readonly HttpClient _http;

    /// <param name="http">
    /// An <see cref="HttpClient"/> whose <c>BaseAddress</c> is the brain base URL and whose handler
    /// pins the brain CA for server-cert validation (see the type remarks). Ownership stays with the
    /// caller / DI container.
    /// </param>
    internal EnrollmentClient(HttpClient http)
    {
        _http = http ?? throw new ArgumentNullException(nameof(http));
    }

    /// <summary>
    /// Build the enrollment payload from <paramref name="signer"/> and POST it to
    /// <c>/device/enroll</c>. Returns the parsed result; a successful enrollment lands the device in
    /// the <c>pending</c> state (see <see cref="EnrollmentResult.IsPending"/>) awaiting an operator
    /// accept — no certificate is issued here.
    /// </summary>
    /// <exception cref="HttpRequestException">The request could not be completed (network/TLS).</exception>
    internal async Task<EnrollmentResult> EnrollAsync(
        string deviceName, TpmClientAuthSigner signer, CancellationToken cancellationToken = default)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(deviceName);
        ArgumentNullException.ThrowIfNull(signer);

        byte[] spkiDer = signer.PublicKeySpkiDer();
        string dpopProof = Convert.ToBase64String(signer.SignToDer(spkiDer));

        var payload = new EnrollmentRequestDto
        {
            Name = deviceName,
            PublicKey = signer.PublicKeySpkiPem(),
            KeyProvenance = signer.KeyProvenance(),
            DpopProof = dpopProof,
        };

        using HttpResponseMessage response = await _http
            .PostAsJsonAsync("/device/enroll", payload, JsonOptions, cancellationToken)
            .ConfigureAwait(false);

        // The route returns 201 on a fresh/idempotent pending row, 422 on a bad payload/PoP, and
        // 429 when the anonymous pending-device cap is hit. All three carry a JSON body.
        EnrollmentResponseDto? body = await response.Content
            .ReadFromJsonAsync<EnrollmentResponseDto>(JsonOptions, cancellationToken)
            .ConfigureAwait(false);

        return EnrollmentResult.From(response.StatusCode, body);
    }

    /// <summary>Wire payload for <c>EnrollmentRequest</c> (extra fields forbidden by the brain).</summary>
    private sealed class EnrollmentRequestDto
    {
        [JsonPropertyName("name")]
        public required string Name { get; init; }

        [JsonPropertyName("public_key")]
        public required string PublicKey { get; init; }

        [JsonPropertyName("key_provenance")]
        public required string KeyProvenance { get; init; }

        // Omit-on-null so we never send `csr`/`attestation`; the brain's model is `extra: forbid`
        // and these optionals default to None — sending only what we set keeps the payload minimal.
        [JsonPropertyName("dpop_proof")]
        [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
        public string? DpopProof { get; init; }
    }

    /// <summary>
    /// Union of the route's success body (<c>device_id</c>, <c>status</c>, <c>public_key_fingerprint</c>)
    /// and its error body (<c>error</c>, <c>detail</c>). Unset fields stay null.
    /// </summary>
    internal sealed class EnrollmentResponseDto
    {
        [JsonPropertyName("device_id")]
        public string? DeviceId { get; init; }

        [JsonPropertyName("status")]
        public string? Status { get; init; }

        [JsonPropertyName("public_key_fingerprint")]
        public string? PublicKeyFingerprint { get; init; }

        [JsonPropertyName("error")]
        public string? Error { get; init; }

        [JsonPropertyName("detail")]
        public string? Detail { get; init; }
    }

    /// <summary>Outcome of an enrollment attempt.</summary>
    internal sealed record EnrollmentResult(
        HttpStatusCode StatusCode,
        string? DeviceId,
        string? Status,
        string? PublicKeyFingerprint,
        string? Error,
        string? Detail)
    {
        /// <summary>True when the device was recorded and is awaiting an operator accept.</summary>
        public bool IsPending =>
            StatusCode == HttpStatusCode.Created &&
            string.Equals(Status, "pending", StringComparison.Ordinal);

        internal static EnrollmentResult From(HttpStatusCode status, EnrollmentResponseDto? body) => new(
            status,
            body?.DeviceId,
            body?.Status,
            body?.PublicKeyFingerprint,
            body?.Error,
            body?.Detail);
    }
}
