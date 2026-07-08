"""The private Cadence certificate authority (EC P-256, vault-sealed root key).

:class:`CadenceCA` is a **minimal private CA** built on ``cryptography.x509``. It mints
short-lived client/server certificates for the mTLS trust fabric, all chaining to a
single self-signed root. The root **private key never touches disk in the clear** — it
is sealed in the NAS-only credential vault (see :mod:`cadence.adapters.vault`) under the
slot ``(provider="cadence_ca", account_ref="root")`` and re-loaded on demand.

Key type is **EC P-256** end to end — uniform with the M8 delegated-signer mTLS spike
(``agents/core/tests/delegated_signer_mtls.rs``: ``PKCS_ECDSA_P256_SHA256`` /
``ECDSA_NISTP256_SHA256``), so a device key minted in a StrongBox/TPM/Secure-Enclave
speaks the same curve and signature scheme the leaf certs are issued for.

Leaf certificates are **short-lived** (default 90 days, configurable) — renewal is a
later step, not built here. There is **no CRL**: revocation is registry-status-based
(a device flips to ``revoked`` in the device registry; enforced by the B2 verification
middleware), not certificate-list-based.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from cadence.adapters.base import CredentialVault
from cadence.config import Settings, get_settings

#: Vault slot the root private key + cert live under.
CA_VAULT_PROVIDER = "cadence_ca"
CA_VAULT_ACCOUNT = "root"

#: Small backdating of ``not_valid_before`` to tolerate modest clock skew between the
#: brain and an enrolling device.
_CLOCK_SKEW = timedelta(minutes=5)

#: The one curve Cadence uses (uniform with the agents mTLS spike).
_CURVE = ec.SECP256R1


def spki_der(public_key: ec.EllipticCurvePublicKey) -> bytes:
    """DER-encoded SubjectPublicKeyInfo for a public key.

    This is the canonical byte-string a device signs to prove possession of its private
    key at enrollment (see :mod:`cadence.devices.enrollment`), and the input to the
    key fingerprint.
    """
    return public_key.public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def spki_pem(public_key: ec.EllipticCurvePublicKey) -> str:
    """PEM-encoded SubjectPublicKeyInfo for a public key (how the registry stores it)."""
    return public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")


def spki_fingerprint(public_key: ec.EllipticCurvePublicKey) -> str:
    """SHA-256 (hex) of the DER SPKI — the stable device-key fingerprint."""
    return hashlib.sha256(spki_der(public_key)).hexdigest()


def cert_fingerprint(cert: x509.Certificate) -> str:
    """SHA-256 (hex) of a certificate's DER encoding."""
    return cert.fingerprint(hashes.SHA256()).hex()


def load_public_key_pem(pem: str | bytes) -> ec.EllipticCurvePublicKey:
    """Parse an SPKI PEM into a P-256 public key, rejecting any other key type/curve.

    Restricting to EC P-256 keeps every enrolled key uniform with the agents mTLS
    spike and the certs this CA issues.
    """
    data = pem.encode("ascii") if isinstance(pem, str) else pem
    try:
        key = serialization.load_pem_public_key(data)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"not a valid PEM public key: {exc}") from exc
    return _require_p256(key)


def _require_p256(key: object) -> ec.EllipticCurvePublicKey:
    if not isinstance(key, ec.EllipticCurvePublicKey):
        raise ValueError("public key must be an EC (P-256) key")
    if not isinstance(key.curve, _CURVE):
        raise ValueError(f"EC key must use curve {_CURVE.name}, got {key.curve.name}")
    return key


class CadenceCA:
    """A minimal, vault-backed private CA that mints short-lived P-256 leaf certs.

    On first use the root CA (self-signed P-256 cert + key) is generated and the root
    **private key is stored in the vault**, not on disk. On subsequent construction the
    root is re-loaded from the vault. All issuance signs with the vault-held root key.
    """

    def __init__(
        self,
        vault: CredentialVault,
        settings: Settings | None = None,
        *,
        root_cn: str = "Cadence Root CA",
    ) -> None:
        self._vault = vault
        self._settings = settings or get_settings()
        self._root_cn = root_cn
        self._root_key, self._root_cert = self._load_or_create_root()

    # -- root lifecycle ----------------------------------------------------- #

    def _load_or_create_root(self) -> tuple[ec.EllipticCurvePrivateKey, x509.Certificate]:
        """Load the root key+cert from the vault, generating+sealing them on first use."""
        try:
            stored = self._vault.get(CA_VAULT_PROVIDER, CA_VAULT_ACCOUNT)
        except KeyError:
            return self._generate_and_seal_root()
        key = serialization.load_pem_private_key(
            stored["private_key_pem"].encode("ascii"), password=None
        )
        if not isinstance(key, ec.EllipticCurvePrivateKey):
            raise ValueError("sealed CA root key is not an EC private key")
        cert = x509.load_pem_x509_certificate(stored["cert_pem"].encode("ascii"))
        return key, cert

    def _generate_and_seal_root(self) -> tuple[ec.EllipticCurvePrivateKey, x509.Certificate]:
        key = ec.generate_private_key(_CURVE())
        now = datetime.now(UTC)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, self._root_cn)])
        cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - _CLOCK_SKEW)
            .not_valid_after(now + timedelta(days=self._settings.ca_root_validity_days))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    content_commitment=False,
                    key_encipherment=False,
                    data_encipherment=False,
                    key_agreement=False,
                    key_cert_sign=True,
                    crl_sign=True,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
            .add_extension(
                x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False
            )
            .sign(key, hashes.SHA256())
        )
        # Seal the PRIVATE key (+ the public cert, for stable re-load) into the vault.
        private_pem = key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode("ascii")
        cert_pem = cert.public_bytes(serialization.Encoding.PEM).decode("ascii")
        self._vault.store(
            CA_VAULT_PROVIDER,
            CA_VAULT_ACCOUNT,
            {"private_key_pem": private_pem, "cert_pem": cert_pem},
        )
        return key, cert

    # -- issuance ----------------------------------------------------------- #

    def issue_client_cert(
        self,
        public_key_or_csr: object,
        subject_cn: str,
        *,
        validity_days: int | None = None,
    ) -> str:
        """Issue a short-lived **clientAuth** leaf cert (PEM) for a device key.

        ``public_key_or_csr`` may be an EC P-256 public key object, a
        :class:`~cryptography.x509.CertificateSigningRequest`, or PEM bytes/str of
        either. The device's private key never reaches the CA — only its public key.
        """
        public_key = self._coerce_public_key(public_key_or_csr)
        cert = self._issue_leaf(
            public_key,
            subject_cn,
            ExtendedKeyUsageOID.CLIENT_AUTH,
            validity_days=validity_days,
        )
        return cert.public_bytes(serialization.Encoding.PEM).decode("ascii")

    def issue_server_cert(
        self,
        public_key_or_csr: object,
        subject_cn: str,
        *,
        validity_days: int | None = None,
        san_dns: list[str] | None = None,
        san_ips: list[str] | None = None,
    ) -> str:
        """Issue a short-lived **serverAuth** leaf cert (PEM), optionally with SANs."""
        public_key = self._coerce_public_key(public_key_or_csr)
        sans: list[x509.GeneralName] = []
        for dns in san_dns or []:
            sans.append(x509.DNSName(dns))
        for ip in san_ips or []:
            import ipaddress

            sans.append(x509.IPAddress(ipaddress.ip_address(ip)))
        cert = self._issue_leaf(
            public_key,
            subject_cn,
            ExtendedKeyUsageOID.SERVER_AUTH,
            validity_days=validity_days,
            sans=sans or None,
        )
        return cert.public_bytes(serialization.Encoding.PEM).decode("ascii")

    def _issue_leaf(
        self,
        public_key: ec.EllipticCurvePublicKey,
        subject_cn: str,
        eku: x509.ObjectIdentifier,
        *,
        validity_days: int | None,
        sans: list[x509.GeneralName] | None = None,
    ) -> x509.Certificate:
        days = validity_days if validity_days is not None else self._settings.ca_leaf_validity_days
        now = datetime.now(UTC)
        builder = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, subject_cn)]))
            .issuer_name(self._root_cert.subject)
            .public_key(public_key)
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - _CLOCK_SKEW)
            .not_valid_after(now + timedelta(days=days))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    content_commitment=False,
                    key_encipherment=False,
                    data_encipherment=False,
                    key_agreement=False,
                    key_cert_sign=False,
                    crl_sign=False,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
            .add_extension(x509.ExtendedKeyUsage([eku]), critical=False)
            .add_extension(
                x509.SubjectKeyIdentifier.from_public_key(public_key), critical=False
            )
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(self._root_cert.public_key()),
                critical=False,
            )
        )
        if sans:
            builder = builder.add_extension(x509.SubjectAlternativeName(sans), critical=False)
        return builder.sign(self._root_key, hashes.SHA256())

    def _coerce_public_key(self, obj: object) -> ec.EllipticCurvePublicKey:
        """Normalize a public-key/CSR/PEM input into a validated P-256 public key."""
        if isinstance(obj, x509.CertificateSigningRequest):
            if not obj.is_signature_valid:
                raise ValueError("CSR signature is invalid")
            return _require_p256(obj.public_key())
        if isinstance(obj, ec.EllipticCurvePublicKey):
            return _require_p256(obj)
        if isinstance(obj, (str, bytes)):
            data = obj.encode("ascii") if isinstance(obj, str) else obj
            # Try CSR PEM first (a CSR PEM would fail public-key parsing), then SPKI PEM.
            try:
                csr = x509.load_pem_x509_csr(data)
            except ValueError:
                return load_public_key_pem(data)
            if not csr.is_signature_valid:
                raise ValueError("CSR signature is invalid")
            return _require_p256(csr.public_key())
        raise TypeError(f"unsupported public-key input: {type(obj).__name__}")

    # -- accessors ---------------------------------------------------------- #

    def ca_cert_pem(self) -> str:
        """PEM of the root CA cert — the pin embedded in agents to verify the brain."""
        return self._root_cert.public_bytes(serialization.Encoding.PEM).decode("ascii")

    def root_fingerprint(self) -> str:
        """SHA-256 (hex) of the root CA cert DER (the pin's fingerprint)."""
        return cert_fingerprint(self._root_cert)


__all__ = [
    "CadenceCA",
    "CA_VAULT_PROVIDER",
    "CA_VAULT_ACCOUNT",
    "spki_der",
    "spki_pem",
    "spki_fingerprint",
    "cert_fingerprint",
    "load_public_key_pem",
]
