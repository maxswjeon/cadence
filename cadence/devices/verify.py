"""Runtime mTLS client-certificate verification (M8 / B2).

:class:`DeviceVerifier` is the enforcement half of the trust fabric that
:mod:`cadence.devices.ca` (issuance) and :mod:`cadence.devices.registry` (trust state)
set up. A TLS-terminating proxy verifies the client certificate at the transport layer
and forwards it to the brain in the ``X-Client-Cert`` header as URL-encoded PEM (nginx
``$ssl_client_escaped_cert`` style). :meth:`DeviceVerifier.verify` re-establishes trust
*inside* the application — it never takes the proxy's word for identity — by:

1. URL-decoding + parsing the forwarded PEM (unparseable → reject).
2. **Chaining the leaf to the Cadence CA**: the CA public key must verify the leaf
   signature, the leaf's validity window must be current, and it must be a *leaf
   clientAuth* cert (EKU clientAuth, ``BasicConstraints CA:FALSE``). A CA cert, a
   serverAuth-only cert, an expired cert, or a self-signed / foreign cert not issued by
   our CA is rejected.
3. **Binding the cert to an accepted device**: the leaf's public-key (SPKI) fingerprint
   must map to a registered device that is status ``accepted`` (``pending`` / ``revoked``
   / unknown → reject), and the leaf's own cert fingerprint must equal the
   ``cert_fingerprint`` the registry recorded when that device was accepted — so a cert
   minted for one device cannot authenticate as another, and a stray cert over the same
   key that was never the accepted one is refused.

Every failure path raises :class:`DeviceVerificationError`; the mTLS gate maps that to a
401. There is deliberately no "allow on doubt" branch — verification is **fail-closed**,
including on *unexpected* errors: :meth:`DeviceVerifier.verify` catches anything (an exotic
cert raising ``UnsupportedAlgorithm``, a registry/DB error) and re-raises it as a
:class:`DeviceVerificationError`, so the gate can only ever answer allow-or-401, never a
500 and never an accidental allow. Revocation is registry-status-based (a device flips to
``revoked``), so there is no CRL to consult here; a revoked device fails the status check.

Trust-boundary caveat (why cert verification is necessary but **not sufficient**)
---------------------------------------------------------------------------------
A client certificate forwarded in a header is **not a secret** — anyone who observes one
can copy it. So verifying the cert proves the *cert* is genuine, not that *this TCP peer*
holds the matching private key (only the TLS terminator that completed the mTLS handshake
knows that). The application therefore must additionally authenticate the proxy hop
(``settings.proxy_shared_secret`` / the ``X-Cadence-Proxy-Auth`` header) so a copied cert
replayed by another peer is refused; see :func:`cadence.brain.app.enforce_mtls`. Without
that secret the API must bind loopback-only behind a same-host proxy (HARD requirement).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import unquote

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID

from cadence.devices.ca import CadenceCA, cert_fingerprint, spki_fingerprint
from cadence.devices.registry import STATUS_ACCEPTED, DeviceRegistry
from cadence.obs.logging import get_logger

_log = get_logger("devices.verify")


class DeviceVerificationError(Exception):
    """The presented client certificate did not verify against the trust fabric.

    Raised on any failure — unparseable cert, broken/foreign chain, expired window,
    wrong cert shape, or an unknown / non-``accepted`` / mismatched device. The gate
    translates it into an HTTP 401 (fail closed).
    """


@dataclass(frozen=True)
class DeviceIdentity:
    """The verified identity of the calling device (id + human label)."""

    id: str
    name: str


class DeviceVerifier:
    """Verifies a forwarded client cert against the Cadence CA + device registry."""

    def __init__(self, ca: CadenceCA, registry: DeviceRegistry) -> None:
        self._registry = registry
        # Snapshot the CA cert once: the leaf's signature is checked against this key and
        # its issuer must match this subject.
        self._ca_cert = x509.load_pem_x509_certificate(ca.ca_cert_pem().encode("ascii"))
        self._ca_public_key = self._ca_cert.public_key()

    def verify(self, header_value: str) -> DeviceIdentity:
        """Return the :class:`DeviceIdentity` for a valid cert, else raise.

        Fail-closed on **anything**: a normal parse/chain/authorization failure raises
        :class:`DeviceVerificationError`, and any *unexpected* error too — an exotic cert
        (e.g. ``UnsupportedAlgorithm`` out of ``signature_hash_algorithm``) or a registry
        / DB error is caught, logged server-side, and re-raised as a
        :class:`DeviceVerificationError` so the gate returns 401, never a 500 and never an
        accidental allow.
        """
        try:
            cert = self._parse(header_value)
            self._verify_chain(cert)
            return self._authorize(cert)
        except DeviceVerificationError:
            raise
        except Exception as exc:  # noqa: BLE001 - fail closed on ANY unexpected error
            _log.warning("client-cert verification failed with unexpected error: %r", exc)
            raise DeviceVerificationError(
                "client certificate could not be verified (unexpected error)"
            ) from exc

    # -- steps -------------------------------------------------------------- #

    @staticmethod
    def _parse(header_value: str) -> x509.Certificate:
        """URL-decode the header and parse it as a PEM certificate (else reject)."""
        if not header_value:
            raise DeviceVerificationError("empty client-certificate header")
        try:
            pem = unquote(header_value).encode("utf-8")
            return x509.load_pem_x509_certificate(pem)
        except (ValueError, TypeError) as exc:
            raise DeviceVerificationError(
                f"client certificate is not a parseable PEM: {exc}"
            ) from exc

    def _verify_chain(self, cert: x509.Certificate) -> None:
        """Verify the leaf chains to the Cadence CA and is a current clientAuth leaf."""
        # Issuer must name our CA (cheap pre-check; the signature below is authoritative).
        if cert.issuer != self._ca_cert.subject:
            raise DeviceVerificationError(
                "client certificate was not issued by the Cadence CA (issuer mismatch)"
            )
        # The CA public key must actually have signed this leaf. This is what rejects a
        # self-signed / foreign cert that merely copied the issuer name.
        try:
            self._ca_public_key.verify(
                cert.signature,
                cert.tbs_certificate_bytes,
                ec.ECDSA(cert.signature_hash_algorithm),
            )
        except (InvalidSignature, ValueError, TypeError) as exc:
            raise DeviceVerificationError(
                "client certificate signature does not verify against the Cadence CA"
            ) from exc
        # Validity window must be current (rejects an expired or not-yet-valid cert).
        now = datetime.now(UTC)
        if now < cert.not_valid_before_utc or now > cert.not_valid_after_utc:
            raise DeviceVerificationError("client certificate is expired or not yet valid")
        # Must be a leaf (CA:FALSE) — a CA cert cannot be used as a client identity.
        try:
            basic = cert.extensions.get_extension_for_class(x509.BasicConstraints).value
        except x509.ExtensionNotFound as exc:
            raise DeviceVerificationError(
                "client certificate has no BasicConstraints extension"
            ) from exc
        if basic.ca:
            raise DeviceVerificationError("a CA certificate cannot authenticate as a device")
        # Must carry the clientAuth EKU — a serverAuth-only cert is not a client identity.
        try:
            eku = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
        except x509.ExtensionNotFound as exc:
            raise DeviceVerificationError(
                "client certificate has no ExtendedKeyUsage extension"
            ) from exc
        if ExtendedKeyUsageOID.CLIENT_AUTH not in eku:
            raise DeviceVerificationError(
                "client certificate is not marked for clientAuth"
            )

    def _authorize(self, cert: x509.Certificate) -> DeviceIdentity:
        """Bind the verified leaf to an ``accepted`` device row (else reject)."""
        # Look the device up by the leaf's public-key (SPKI) fingerprint — the stable key
        # the registry indexes enrolled devices under.
        device = self._registry.by_fingerprint(spki_fingerprint(cert.public_key()))
        if device is None:
            raise DeviceVerificationError("no enrolled device matches the certificate key")
        if device.status != STATUS_ACCEPTED:
            raise DeviceVerificationError(
                f"device {device.id!r} is not accepted (status {device.status!r})"
            )
        # The presented cert must be *the* cert recorded for this device on accept — a
        # different cert over the same key (or a cert for another device) is refused.
        if device.cert_fingerprint != cert_fingerprint(cert):
            raise DeviceVerificationError(
                "certificate fingerprint does not match the device's accepted certificate"
            )
        return DeviceIdentity(id=device.id, name=device.name)


__all__ = ["DeviceVerifier", "DeviceVerificationError", "DeviceIdentity"]
