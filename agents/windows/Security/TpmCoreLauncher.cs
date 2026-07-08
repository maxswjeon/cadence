using System.Runtime.Versioning;
using System.Text.Json;
using System.Text.Json.Serialization;
using Cadence.WindowsAgent.Interop;

namespace Cadence.WindowsAgent.Security;

/// <summary>
/// Post-enrollment bring-up: once an operator has accepted this device and delivered its client
/// certificate chain, this launcher initializes the Rust core with the TPM-backed signer instead of
/// an exportable PEM key.
///
/// <para><b>Full device bring-up sequence</b></para>
/// <list type="number">
///   <item>Create/open the TPM key: <c>new <see cref="TpmClientAuthSigner"/>()</c>.</item>
///   <item>Enroll: <c><see cref="EnrollmentClient"/>.EnrollAsync(name, signer)</c> → a <c>pending</c>
///     device row on the brain. No cert yet; this endpoint is powerless by design.</item>
///   <item><b>Out of band:</b> a human operator reviews the pending device (the B2 operator CLI)
///     and accepts it, which mints and delivers the client certificate chain (leaf first).</item>
///   <item>Bring up the transport with the hardware signer:
///     <c><see cref="Launch"/>(config, certChainPem, signer)</c>, which calls
///     <see cref="CoreInterop.CadenceCoreInitWithSigner"/>. The private key never leaves the TPM;
///     the core calls back into <see cref="TpmClientAuthSigner.Callback"/> for each mTLS handshake.</item>
/// </list>
///
/// <para>
/// The returned <see cref="CoreSignerHandle"/> keeps the signer and its callback delegate rooted for
/// the whole life of the native handle (the core stores the raw function pointer). Dispose it to
/// shut the core down; keep it alive for as long as the agent captures/drains.
/// </para>
/// </summary>
[SupportedOSPlatform("windows")]
internal static class TpmCoreLauncher
{
    /// <summary>
    /// Compose the <c>cadence_core_init_with_signer</c> config JSON and initialize the core with the
    /// TPM signer. <paramref name="baseConfig"/> carries the non-secret transport settings;
    /// <paramref name="certChainPem"/> is the operator-delivered client cert chain (leaf first, NO
    /// private key) and becomes <c>cert_chain_pem</c>.
    /// </summary>
    /// <exception cref="InvalidOperationException">The core rejected the config (handle came back 0).</exception>
    internal static CoreSignerHandle Launch(
        CoreSignerConfig baseConfig, string certChainPem, TpmClientAuthSigner signer)
    {
        ArgumentNullException.ThrowIfNull(baseConfig);
        ArgumentException.ThrowIfNullOrWhiteSpace(certChainPem);
        ArgumentNullException.ThrowIfNull(signer);

        string configJson = baseConfig.ToJson(certChainPem);

        // Root the delegate BEFORE the native call and hand the same instance to the core.
        CoreInterop.CadenceSignCallback callback = signer.Callback;

        // ctx is unused: `callback` is an instance method closed over `signer`, so the managed side
        // already has all the state it needs. We pass nint.Zero and the callback ignores its ctx arg.
        nint handle = CoreInterop.CadenceCoreInitWithSigner(configJson, callback, nint.Zero);
        if (handle == nint.Zero)
        {
            throw new InvalidOperationException(
                "cadence_core_init_with_signer failed: " + CoreErrors.ReadLastError());
        }

        return new CoreSignerHandle(handle, signer, callback);
    }

    /// <summary>
    /// Non-secret transport config shared with <c>cadence_core_init</c>, minus
    /// <c>client_identity_pem</c>. <see cref="ToJson"/> adds <c>cert_chain_pem</c> to produce the
    /// <c>cadence_core_init_with_signer</c> shape.
    /// </summary>
    internal sealed class CoreSignerConfig
    {
        public required string WalPath { get; init; }
        public required uint Capacity { get; init; }
        public required string BaseUrl { get; init; }
        public required string CaPem { get; init; }
        public RetryConfig? Retry { get; init; }

        internal string ToJson(string certChainPem)
        {
            var dto = new ConfigDto
            {
                WalPath = WalPath,
                Capacity = Capacity,
                BaseUrl = BaseUrl,
                CertChainPem = certChainPem,
                CaPem = CaPem,
                Retry = Retry,
            };
            return JsonSerializer.Serialize(dto);
        }
    }

    internal sealed class RetryConfig
    {
        [JsonPropertyName("base_ms")]
        public required uint BaseMs { get; init; }

        [JsonPropertyName("max_ms")]
        public required uint MaxMs { get; init; }

        [JsonPropertyName("max_attempts")]
        public required uint MaxAttempts { get; init; }
    }

    private sealed class ConfigDto
    {
        [JsonPropertyName("wal_path")]
        public required string WalPath { get; init; }

        [JsonPropertyName("capacity")]
        public required uint Capacity { get; init; }

        [JsonPropertyName("base_url")]
        public required string BaseUrl { get; init; }

        [JsonPropertyName("cert_chain_pem")]
        public required string CertChainPem { get; init; }

        [JsonPropertyName("ca_pem")]
        public required string CaPem { get; init; }

        [JsonPropertyName("retry")]
        [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
        public RetryConfig? Retry { get; init; }
    }
}

/// <summary>
/// Owns a core handle built with a TPM signer, keeping the signer and its callback delegate rooted
/// for the handle's lifetime. Disposing shuts the core down (which frees the native handle) and
/// disposes the signer; do not use the handle afterward.
/// </summary>
[SupportedOSPlatform("windows")]
internal sealed class CoreSignerHandle : IDisposable
{
    // Held solely to keep the delegate reachable by the GC while the core holds its function pointer.
    private readonly CoreInterop.CadenceSignCallback _rootedCallback;
    private readonly TpmClientAuthSigner _signer;
    private bool _disposed;

    internal CoreSignerHandle(nint handle, TpmClientAuthSigner signer, CoreInterop.CadenceSignCallback rootedCallback)
    {
        Handle = handle;
        _signer = signer;
        _rootedCallback = rootedCallback;
    }

    /// <summary>The opaque <c>CadenceCore*</c> to pass to capture/drain/pending calls on <c>CoreInterop</c>.</summary>
    internal nint Handle { get; private set; }

    public void Dispose()
    {
        if (_disposed)
        {
            return;
        }
        _disposed = true;

        if (Handle != nint.Zero)
        {
            CoreInterop.CadenceCoreShutdown(Handle);
            Handle = nint.Zero;
        }
        // Only now is it safe to drop the signer/delegate — the core no longer holds the pointer.
        _signer.Dispose();
        GC.KeepAlive(_rootedCallback);
    }
}
