"""Shared helpers for the M8/B2 mTLS DeviceVerifier tests.

Build a real trust chain over the test vault + in-memory D1: a Cadence CA, a device
registry, an enrolled+accepted device with a real CA-issued client cert, and the
URL-encoded ``X-Client-Cert`` header a TLS-terminating proxy would forward.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from cadence.adapters.vault import FileCredentialVault
from cadence.devices.ca import CadenceCA, cert_fingerprint, spki_fingerprint, spki_pem
from cadence.devices.registry import DeviceRegistry
from cadence.devices.verify import DeviceVerifier
from cadence.stores.models import Device


def new_key() -> ec.EllipticCurvePrivateKey:
    """A fresh EC P-256 device key."""
    return ec.generate_private_key(ec.SECP256R1())


def escaped_pem(pem: str) -> str:
    """URL-encode a PEM the way nginx's ``$ssl_client_escaped_cert`` does."""
    return quote(pem, safe="")


@dataclass
class TrustFabric:
    """A wired CA + registry + verifier over one store, plus device/cert helpers."""

    ca: CadenceCA
    registry: DeviceRegistry
    verifier: DeviceVerifier

    def enroll(self, key: ec.EllipticCurvePrivateKey, *, name: str = "device") -> Device:
        return self.registry.enroll_request(
            name=name,
            public_key_pem=spki_pem(key.public_key()),
            public_key_fingerprint=spki_fingerprint(key.public_key()),
        )

    def issued_cert(self, key: ec.EllipticCurvePrivateKey, *, cn: str = "device") -> str:
        """A CA-issued clientAuth leaf PEM for ``key`` (not yet bound to a device)."""
        return self.ca.issue_client_cert(key.public_key(), cn)

    def accepted_device_cert(
        self, key: ec.EllipticCurvePrivateKey, *, name: str = "device"
    ) -> tuple[Device, str]:
        """Enroll ``key``, issue its client cert, ACCEPT the device on that cert.

        Returns ``(device, cert_pem)`` — the happy path an authenticated caller presents.
        """
        device = self.enroll(key, name=name)
        cert_pem = self.issued_cert(key, cn=name)
        leaf = x509.load_pem_x509_certificate(cert_pem.encode("ascii"))
        self.registry.accept(device.id, cert_fingerprint=cert_fingerprint(leaf))
        return device, cert_pem


def build_trust_fabric(store, settings) -> TrustFabric:
    """Assemble a real :class:`DeviceVerifier` (CA over the test vault + registry)."""
    ca = CadenceCA(FileCredentialVault(settings), settings)
    registry = DeviceRegistry(store)
    return TrustFabric(ca=ca, registry=registry, verifier=DeviceVerifier(ca, registry))


def self_signed_cert(key: ec.EllipticCurvePrivateKey, *, cn: str = "rogue") -> str:
    """A self-signed clientAuth leaf NOT issued by our CA (a foreign cert)."""
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False)
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM).decode("ascii")


def expired_ca_issued_cert(
    fabric: TrustFabric, key: ec.EllipticCurvePrivateKey, *, cn: str = "device"
) -> str:
    """A properly CA-signed clientAuth leaf whose validity window is already past.

    Signed with the real CA root key (so the chain/signature check passes) but with a
    ``not_valid_after`` in the past, to exercise the validity-window rejection in
    isolation from the chain check.
    """
    root_key = fabric.ca._root_key  # noqa: SLF001 - test needs the CA key to backdate a leaf
    root_cert = fabric.ca._root_cert  # noqa: SLF001
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)]))
        .issuer_name(root_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=10))
        .not_valid_after(now - timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False)
        .sign(root_key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM).decode("ascii")
