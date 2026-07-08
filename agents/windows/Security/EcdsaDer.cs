namespace Cadence.WindowsAgent.Security;

/// <summary>
/// Converts a raw IEEE&#160;P1363 ECDSA signature (fixed-width <c>r || s</c>) into the ASN.1-DER
/// <c>SEQUENCE { INTEGER r, INTEGER s }</c> encoding (RFC&#160;3279 / X9.62) that the Cadence core's
/// <see cref="Cadence.WindowsAgent.Interop.CoreInterop.CadenceSignCallback"/> must write, and that
/// the brain's enrollment PoP check (<c>cadence/devices/enrollment.py</c> — <c>ec.ECDSA(SHA256)</c>
/// verify) expects.
///
/// <para>
/// Windows CNG (<c>ECDsaCng.SignHash</c> / <c>NCryptSignHash</c>) returns the P1363 form: for
/// P-256, exactly 64 bytes = the 32-byte big-endian <c>r</c> followed by the 32-byte big-endian
/// <c>s</c>. DER instead encodes each of <c>r</c> and <c>s</c> as a minimal-length signed
/// <c>INTEGER</c>: leading <c>0x00</c> bytes are stripped, but a single <c>0x00</c> pad byte is
/// prepended when the top bit of the first magnitude byte is set (so the value is not misread as
/// negative). This is a pure, hardware-free function so it is unit-testable without a TPM.
/// </para>
///
/// <para>
/// .NET 5+ also offers <c>ECDsa.SignHash(hash, DSASignatureFormat.Rfc3279DerSequence)</c>, which
/// would emit DER directly; the seam (agents/core header) nonetheless specifies the P1363→DER
/// conversion explicitly, and keeping it as an auditable, independently testable function is the
/// point — so we implement it here rather than lean on the framework's opaque path.
/// </para>
/// </summary>
internal static class EcdsaDer
{
    /// <summary>DER max length for a P-256 signature: SEQUENCE(2) + 2×INTEGER(2 hdr + 33 body).</summary>
    internal const int MaxP256DerLength = 72;

    /// <summary>
    /// Convert <paramref name="p1363"/> (concatenated big-endian <c>r || s</c>, even length, each
    /// half the curve's field size — 32 bytes for P-256) into its ASN.1-DER encoding.
    /// </summary>
    /// <exception cref="ArgumentException">
    /// The input is empty or has an odd length (it cannot be split into equal <c>r</c>/<c>s</c>).
    /// </exception>
    internal static byte[] ConvertP1363ToDer(ReadOnlySpan<byte> p1363)
    {
        if (p1363.Length == 0 || (p1363.Length & 1) != 0)
        {
            throw new ArgumentException(
                $"P1363 ECDSA signature must be a non-empty even length (r||s); got {p1363.Length}.",
                nameof(p1363));
        }

        int half = p1363.Length / 2;
        byte[] r = EncodeUnsignedInteger(p1363[..half]);
        byte[] s = EncodeUnsignedInteger(p1363[half..]);

        // SEQUENCE body = the two DER INTEGER TLVs concatenated.
        int bodyLen = r.Length + s.Length;

        // For P-256 the body is <= 70 bytes, so the SEQUENCE length is always a single short-form
        // byte. Encode the length generally anyway so the helper is correct for any curve size.
        byte[] lenBytes = EncodeLength(bodyLen);

        var der = new byte[1 + lenBytes.Length + bodyLen];
        int i = 0;
        der[i++] = 0x30; // SEQUENCE tag
        Array.Copy(lenBytes, 0, der, i, lenBytes.Length);
        i += lenBytes.Length;
        Array.Copy(r, 0, der, i, r.Length);
        i += r.Length;
        Array.Copy(s, 0, der, i, s.Length);
        return der;
    }

    /// <summary>
    /// Encode one big-endian magnitude as a DER <c>INTEGER</c> TLV: strip surplus leading zero
    /// bytes (keeping at least one), then prepend a <c>0x00</c> sign byte when the high bit of the
    /// first remaining byte is set so the two's-complement value stays non-negative.
    /// </summary>
    private static byte[] EncodeUnsignedInteger(ReadOnlySpan<byte> magnitude)
    {
        int start = 0;
        while (start < magnitude.Length - 1 && magnitude[start] == 0x00)
        {
            start++;
        }

        ReadOnlySpan<byte> trimmed = magnitude[start..];
        bool needsSignByte = (trimmed[0] & 0x80) != 0;
        int contentLen = trimmed.Length + (needsSignByte ? 1 : 0);

        byte[] lenBytes = EncodeLength(contentLen);
        var tlv = new byte[1 + lenBytes.Length + contentLen];
        int i = 0;
        tlv[i++] = 0x02; // INTEGER tag
        Array.Copy(lenBytes, 0, tlv, i, lenBytes.Length);
        i += lenBytes.Length;
        if (needsSignByte)
        {
            tlv[i++] = 0x00;
        }
        trimmed.CopyTo(tlv.AsSpan(i));
        return tlv;
    }

    /// <summary>
    /// DER definite-length encoding: short form (one byte) for lengths &lt; 128, otherwise long form
    /// (<c>0x80 | byteCount</c> followed by the big-endian length). P-256 only ever hits the short
    /// form; the long form is here for correctness on larger curves.
    /// </summary>
    private static byte[] EncodeLength(int length)
    {
        if (length < 0x80)
        {
            return new[] { (byte)length };
        }

        // Big-endian minimal-byte length.
        int n = length;
        int byteCount = 0;
        while (n > 0)
        {
            byteCount++;
            n >>= 8;
        }

        var buf = new byte[1 + byteCount];
        buf[0] = (byte)(0x80 | byteCount);
        for (int i = 0; i < byteCount; i++)
        {
            buf[buf.Length - 1 - i] = (byte)(length >> (8 * i));
        }
        return buf;
    }
}
