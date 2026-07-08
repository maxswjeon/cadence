using System.Runtime.InteropServices;
using System.Runtime.Versioning;
using System.Security.Cryptography;
using Cadence.WindowsAgent.Interop;

namespace Cadence.WindowsAgent.Security;

/// <summary>
/// A hardware-backed mTLS client-auth signer for the Cadence Windows agent. The client private key
/// is a non-exportable P-256 key created inside the TPM via CNG's <b>Microsoft Platform Crypto
/// Provider</b> (KSP); it never leaves the secure element. This type supplies the
/// <see cref="CoreInterop.CadenceSignCallback"/> the Rust core invokes during the mTLS handshake
/// (<see cref="CoreInterop.CadenceCoreInitWithSigner"/>), plus the public-key material and proof
/// the device-enrollment flow needs (<see cref="PublicKeySpkiPem"/>, <see cref="SignToDer"/>).
///
/// <para>
/// <b>Platform floor.</b> ECC (P-256) keys in the Platform Crypto Provider require a discrete/fTPM
/// 2.0 and a recent OS; treat <b>Windows 10 21H2+ / Windows 11</b> as the supported floor. On older
/// builds (or a machine with no usable TPM) <see cref="CngKey.Create(CngAlgorithm, string,
/// CngKeyCreationParameters)"/> throws <see cref="CryptographicException"/> — the caller should
/// surface that as "this device cannot enroll with a hardware key" rather than silently falling
/// back to a software key.
/// </para>
///
/// <para>
/// <b>Lifetime / threading.</b> The instance owns the <see cref="ECDsaCng"/> + <see cref="CngKey"/>
/// and MUST outlive any core handle built from <see cref="Callback"/> (the Rust core calls the
/// callback on an internal transport thread). <see cref="Callback"/> is a single cached delegate
/// instance so that, as long as this signer is rooted, the unmanaged function pointer the core
/// holds stays valid. <c>SignHash</c> against one CNG key handle is serialized under
/// <see cref="_signLock"/> because concurrent NCrypt operations on a single key handle are not
/// guaranteed safe.
/// </para>
///
/// <para>TPM/hardware validation pending: the CNG create/open and TPM signing paths cannot be
/// exercised in this repo (no Windows/TPM here); they are written against the documented CNG APIs
/// and must be validated on real Windows 11 + TPM 2.0 hardware.</para>
///
/// <para>CNG API references (checked at write time):
/// <list type="bullet">
///   <item>CngKey.Create — https://learn.microsoft.com/dotnet/api/system.security.cryptography.cngkey.create</item>
///   <item>CngProvider.MicrosoftPlatformCryptoProvider — https://learn.microsoft.com/dotnet/api/system.security.cryptography.cngprovider.microsoftplatformcryptoprovider</item>
///   <item>ECDsaCng.SignHash — https://learn.microsoft.com/dotnet/api/system.security.cryptography.ecdsacng.signhash</item>
///   <item>Platform Crypto Provider — https://learn.microsoft.com/windows/security/hardware-security/tpm/tpm-fundamentals</item>
/// </list>
/// </para>
/// </summary>
[SupportedOSPlatform("windows")]
internal sealed class TpmClientAuthSigner : IDisposable
{
    /// <summary>
    /// Default CNG key name for the agent's device-identity key. A stable name lets the signer
    /// open the same TPM key across process restarts (create-if-absent, open-if-present).
    /// </summary>
    internal const string DefaultKeyName = "Cadence.Device.ClientAuth.P256";

    /// <summary>
    /// Wire value for the enrollment payload's <c>key_provenance</c>. The brain's
    /// <c>EnrollmentRequest</c> constrains this to <c>Literal["hardware","software","unknown"]</c>
    /// (<c>cadence/devices/enrollment.py</c>), so a TPM key reports the canonical <c>"hardware"</c>;
    /// the finer-grained "TPM Platform Crypto Provider" detail is carried in the doc/attestation
    /// path, not this enum. Sending <c>"tpm"</c> here would be rejected with HTTP 422.
    /// </summary>
    internal const string KeyProvenanceHardware = "hardware";

    private readonly ECDsaCng _ecdsa;
    private readonly CngKey _key;
    private readonly object _signLock = new();
    private bool _disposed;

    /// <summary>
    /// Open the agent's device-identity key in the TPM (Platform Crypto Provider), creating a new
    /// non-exportable P-256 key on first run and re-opening it thereafter.
    /// </summary>
    /// <param name="keyName">CNG key name; defaults to <see cref="DefaultKeyName"/>.</param>
    /// <exception cref="CryptographicException">
    /// No usable TPM / Platform Crypto Provider, or the OS is below the ECC floor.
    /// </exception>
    internal TpmClientAuthSigner(string keyName = DefaultKeyName)
    {
        _key = OpenOrCreateKey(keyName);
        _ecdsa = new ECDsaCng(_key);
        Callback = Sign; // cache one delegate instance so the native function pointer stays valid.
    }

    /// <summary>
    /// The cached <see cref="CoreInterop.CadenceSignCallback"/> to hand to
    /// <see cref="CoreInterop.CadenceCoreInitWithSigner"/>. Rooted for as long as this signer is
    /// (see the type remarks on lifetime).
    /// </summary>
    internal CoreInterop.CadenceSignCallback Callback { get; }

    private static CngKey OpenOrCreateKey(string keyName)
    {
        var provider = CngProvider.MicrosoftPlatformCryptoProvider;
        if (CngKey.Exists(keyName, provider))
        {
            return CngKey.Open(keyName, provider);
        }

        var creationParameters = new CngKeyCreationParameters
        {
            Provider = provider,
            // Non-exportable is the default, but state it explicitly: the whole point is that the
            // private key can never leave the TPM.
            ExportPolicy = CngExportPolicies.None,
            KeyCreationOptions = CngKeyCreationOptions.None,
            KeyUsage = CngKeyUsages.Signing,
        };

        // TPM/hardware validation pending: exercises the Platform Crypto Provider; requires
        // Windows 10 21H2+/11 with TPM 2.0 for ECC. Throws CryptographicException otherwise.
        return CngKey.Create(CngAlgorithm.ECDsaP256, keyName, creationParameters);
    }

    /// <summary>
    /// The device public key as a SubjectPublicKeyInfo PEM (<c>-----BEGIN PUBLIC KEY-----</c>) — the
    /// enrollment payload's <c>public_key</c> field.
    /// </summary>
    internal string PublicKeySpkiPem()
    {
        ThrowIfDisposed();
        return _ecdsa.ExportSubjectPublicKeyInfoPem();
    }

    /// <summary>The device public key as raw DER SubjectPublicKeyInfo bytes (what the DPoP proof signs).</summary>
    internal byte[] PublicKeySpkiDer()
    {
        ThrowIfDisposed();
        return _ecdsa.ExportSubjectPublicKeyInfo();
    }

    /// <summary>Wire <c>key_provenance</c> for enrollment — see <see cref="KeyProvenanceHardware"/>.</summary>
    internal string KeyProvenance() => KeyProvenanceHardware;

    /// <summary>
    /// SHA-256 <paramref name="message"/>, sign it with the TPM key, and return the ASN.1-DER
    /// ECDSA-P256 signature. Used both by the mTLS <see cref="Callback"/> and to build the
    /// enrollment DPoP proof (signing the device's own DER SPKI).
    /// </summary>
    internal byte[] SignToDer(ReadOnlySpan<byte> message)
    {
        ThrowIfDisposed();
        byte[] digest = SHA256.HashData(message);
        byte[] p1363;
        lock (_signLock)
        {
            // ECDsaCng.SignHash returns the raw IEEE-P1363 r||s form (64 bytes for P-256).
            // TPM/hardware validation pending: the actual NCryptSignHash into the TPM.
            p1363 = _ecdsa.SignHash(digest);
        }
        return EcdsaDer.ConvertP1363ToDer(p1363);
    }

    /// <summary>
    /// The <see cref="CoreInterop.CadenceSignCallback"/> implementation. Fails closed (returns a
    /// non-zero status) on any error — a thrown exception must never cross the unmanaged boundary.
    /// </summary>
    private int Sign(nint ctx, nint msg, nuint msgLen, nint outSig, nuint outSigCap, out nuint outSigLen)
    {
        outSigLen = 0;
        try
        {
            if (msg == nint.Zero || outSig == nint.Zero)
            {
                return 1;
            }

            var message = new byte[(int)msgLen];
            if (message.Length > 0)
            {
                Marshal.Copy(msg, message, 0, message.Length);
            }

            byte[] der = SignToDer(message);
            if ((nuint)der.Length > outSigCap)
            {
                // Caller guarantees cap >= 72; a P-256 DER signature is <= 72. Fail closed anyway.
                return 2;
            }

            Marshal.Copy(der, 0, outSig, der.Length);
            outSigLen = (nuint)der.Length;
            return 0;
        }
        catch
        {
            // Any failure (TPM unavailable, key gone, marshalling error) fails the handshake closed.
            return 3;
        }
    }

    private void ThrowIfDisposed()
    {
        ObjectDisposedException.ThrowIf(_disposed, this);
    }

    public void Dispose()
    {
        if (_disposed)
        {
            return;
        }
        _disposed = true;
        _ecdsa.Dispose();
        _key.Dispose();
    }
}
