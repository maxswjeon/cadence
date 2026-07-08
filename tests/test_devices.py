"""Tests for the M8/B1 device trust fabric: private CA, registry, enrollment intake."""

from __future__ import annotations

import base64

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from fastapi.testclient import TestClient
from pydantic import ValidationError

from cadence.adapters.vault import FileCredentialVault
from cadence.brain.app import create_app
from cadence.devices.ca import (
    CA_VAULT_ACCOUNT,
    CA_VAULT_PROVIDER,
    CadenceCA,
    spki_der,
    spki_fingerprint,
    spki_pem,
)
from cadence.devices.enrollment import (
    MAX_PUBLIC_KEY_LEN,
    EnrollmentError,
    EnrollmentRequest,
    EnrollmentService,
)
from cadence.devices.registry import (
    STATUS_ACCEPTED,
    STATUS_PENDING,
    STATUS_REVOKED,
    DeviceRegistry,
    InvalidTransition,
    PendingCapExceeded,
)
from cadence.ingest.pipeline import IngestPipeline

# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _new_device_key() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


def _spki_pem_of(key: ec.EllipticCurvePrivateKey) -> str:
    return spki_pem(key.public_key())


def _make_csr(key: ec.EllipticCurvePrivateKey, cn: str = "device-1") -> str:
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)]))
        .sign(key, hashes.SHA256())
    )
    return csr.public_bytes(serialization.Encoding.PEM).decode("ascii")


def _dpop_proof(key: ec.EllipticCurvePrivateKey) -> str:
    """A valid proof of possession: base64 ECDSA sig by the key over its own DER SPKI."""
    sig = key.sign(spki_der(key.public_key()), ec.ECDSA(hashes.SHA256()))
    return base64.b64encode(sig).decode("ascii")


def _enroll_payload(key: ec.EllipticCurvePrivateKey, **extra) -> dict:
    """A minimal valid enrollment body (public key + DPoP proof of possession)."""
    body = {"name": "device", "public_key": _spki_pem_of(key), "dpop_proof": _dpop_proof(key)}
    body.update(extra)
    return body


@pytest.fixture
def vault(settings) -> FileCredentialVault:
    return FileCredentialVault(settings)


@pytest.fixture
def ca(vault, settings) -> CadenceCA:
    return CadenceCA(vault, settings)


@pytest.fixture
def registry(store) -> DeviceRegistry:
    return DeviceRegistry(store)


# --------------------------------------------------------------------------- #
# CA — issuance, chain verification, curve
# --------------------------------------------------------------------------- #


def test_ca_issues_client_cert_that_chains_and_is_p256(ca) -> None:
    device_key = _new_device_key()
    pem = ca.issue_client_cert(device_key.public_key(), "device-1")
    leaf = x509.load_pem_x509_certificate(pem.encode("ascii"))

    # Leaf key is EC P-256.
    assert isinstance(leaf.public_key(), ec.EllipticCurvePublicKey)
    assert isinstance(leaf.public_key().curve, ec.SECP256R1)

    # clientAuth EKU, not a CA.
    eku = leaf.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    assert ExtendedKeyUsageOID.CLIENT_AUTH in eku
    bc = leaf.extensions.get_extension_for_class(x509.BasicConstraints).value
    assert bc.ca is False

    # Chain builds: the root's public key verifies the leaf signature.
    ca_cert = x509.load_pem_x509_certificate(ca.ca_cert_pem().encode("ascii"))
    ca_cert.public_key().verify(
        leaf.signature, leaf.tbs_certificate_bytes, ec.ECDSA(leaf.signature_hash_algorithm)
    )
    assert leaf.issuer == ca_cert.subject


def test_ca_issues_server_cert_with_sans(ca) -> None:
    key = _new_device_key()
    pem = ca.issue_server_cert(
        key.public_key(), "brain.local", san_dns=["brain.local"], san_ips=["127.0.0.1"]
    )
    leaf = x509.load_pem_x509_certificate(pem.encode("ascii"))
    eku = leaf.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    assert ExtendedKeyUsageOID.SERVER_AUTH in eku
    san = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert "brain.local" in san.get_values_for_type(x509.DNSName)


def test_ca_issues_from_csr(ca) -> None:
    key = _new_device_key()
    csr_pem = _make_csr(key)
    pem = ca.issue_client_cert(csr_pem, "device-csr")
    leaf = x509.load_pem_x509_certificate(pem.encode("ascii"))
    # Cert public key matches the CSR/device key.
    assert spki_fingerprint(leaf.public_key()) == spki_fingerprint(key.public_key())


def test_ca_default_leaf_validity_is_short_lived(ca, settings) -> None:
    key = _new_device_key()
    leaf = x509.load_pem_x509_certificate(
        ca.issue_client_cert(key.public_key(), "d").encode("ascii")
    )
    span = leaf.not_valid_after_utc - leaf.not_valid_before_utc
    # 90-day default (+ the small clock-skew backdate), well under a year.
    assert settings.ca_leaf_validity_days == 90
    assert 90 <= span.days <= 91


def test_ca_rejects_non_p256_key(ca) -> None:
    rsa_pub = _rsa_public_pem()
    with pytest.raises(ValueError):
        ca.issue_client_cert(rsa_pub, "bad")


# --------------------------------------------------------------------------- #
# CA — root key is sealed in the vault, never on disk in the clear
# --------------------------------------------------------------------------- #


def test_ca_root_key_round_trips_through_vault(vault, settings) -> None:
    ca1 = CadenceCA(vault, settings)
    fp1 = ca1.root_fingerprint()

    # The root PRIVATE key is stored in the vault under (cadence_ca, root).
    stored = vault.get(CA_VAULT_PROVIDER, CA_VAULT_ACCOUNT)
    assert "private_key_pem" in stored
    assert "PRIVATE KEY" in stored["private_key_pem"]

    # A fresh CA over the same vault re-loads the identical root (same fingerprint),
    # rather than minting a new one.
    ca2 = CadenceCA(vault, settings)
    assert ca2.root_fingerprint() == fp1
    assert ca2.ca_cert_pem() == ca1.ca_cert_pem()


def test_ca_root_private_key_not_written_to_disk_in_clear(vault, settings) -> None:
    ca = CadenceCA(vault, settings)
    priv_pem = _root_private_pem(vault)
    # Walk the whole vault dir: the raw private-key PEM must not appear in any file
    # (the vault seals records with AES-256-GCM).
    needle = priv_pem.strip().encode("ascii")
    for path in settings.vault_dir.rglob("*"):
        if path.is_file():
            assert needle not in path.read_bytes()
    assert ca.root_fingerprint()  # sanity: CA is usable


# --------------------------------------------------------------------------- #
# Device registry — transitions + fingerprint idempotency
# --------------------------------------------------------------------------- #


def test_registry_enroll_creates_pending(registry) -> None:
    key = _new_device_key()
    dev = registry.enroll_request(
        name="phone",
        public_key_pem=_spki_pem_of(key),
        public_key_fingerprint=spki_fingerprint(key.public_key()),
        key_provenance="hardware",
    )
    assert dev.status == STATUS_PENDING
    assert dev.cert_fingerprint is None
    assert dev.decided_at is None
    assert dev.key_provenance == "hardware"
    assert registry.get(dev.id).status == STATUS_PENDING


def test_registry_enroll_is_fingerprint_idempotent(registry) -> None:
    key = _new_device_key()
    fp = spki_fingerprint(key.public_key())
    d1 = registry.enroll_request(
        name="a", public_key_pem=_spki_pem_of(key), public_key_fingerprint=fp
    )
    d2 = registry.enroll_request(
        name="b", public_key_pem=_spki_pem_of(key), public_key_fingerprint=fp
    )
    assert d1.id == d2.id
    assert len(registry.list()) == 1
    # The first row wins (no silent overwrite of the name).
    assert registry.get(d1.id).name == "a"


def test_registry_accept_and_revoke_transitions(registry) -> None:
    key = _new_device_key()
    dev = registry.enroll_request(
        name="laptop", public_key_pem=_spki_pem_of(key),
        public_key_fingerprint=spki_fingerprint(key.public_key()),
    )
    accepted = registry.accept(dev.id, cert_fingerprint="abc123")
    assert accepted.status == STATUS_ACCEPTED
    assert accepted.cert_fingerprint == "abc123"
    assert accepted.decided_at is not None

    revoked = registry.revoke(dev.id)
    assert revoked.status == STATUS_REVOKED
    assert revoked.decided_at is not None


def test_registry_list_filters_by_status(registry) -> None:
    keys = [_new_device_key() for _ in range(3)]
    devs = [
        registry.enroll_request(
            name=f"d{i}", public_key_pem=_spki_pem_of(k),
            public_key_fingerprint=spki_fingerprint(k.public_key()),
        )
        for i, k in enumerate(keys)
    ]
    registry.accept(devs[0].id, cert_fingerprint="f0")
    assert {d.id for d in registry.list(status=STATUS_PENDING)} == {devs[1].id, devs[2].id}
    assert {d.id for d in registry.list(status=STATUS_ACCEPTED)} == {devs[0].id}


def test_registry_by_fingerprint(registry) -> None:
    key = _new_device_key()
    fp = spki_fingerprint(key.public_key())
    dev = registry.enroll_request(
        name="d", public_key_pem=_spki_pem_of(key), public_key_fingerprint=fp
    )
    assert registry.by_fingerprint(fp).id == dev.id
    assert registry.by_fingerprint("deadbeef") is None


def test_registry_accept_unknown_device_raises(registry) -> None:
    with pytest.raises(KeyError):
        registry.accept("nonexistent", cert_fingerprint="x")


def test_registry_writes_do_not_reach_replica(registry, store) -> None:
    # Device/security metadata is local-only — it must never enter the Cloudflare
    # replication queue (its public-key PEM would also trip the raw-boundary blob check).
    key = _new_device_key()
    registry.enroll_request(
        name="d", public_key_pem=_spki_pem_of(key),
        public_key_fingerprint=spki_fingerprint(key.public_key()),
    )
    assert store.replica.queue_depth == 0


# --------------------------------------------------------------------------- #
# Enrollment service — validation
# --------------------------------------------------------------------------- #


def test_enrollment_service_records_pending_with_dpop(registry) -> None:
    key = _new_device_key()
    proof = _dpop_proof(key)  # ECDSA is randomized; capture the exact proof submitted.
    service = EnrollmentService(registry)
    dev = service.enroll(
        EnrollmentRequest(
            name="phone", public_key=_spki_pem_of(key),
            key_provenance="hardware", dpop_proof=proof,
        )
    )
    assert dev.status == STATUS_PENDING
    assert dev.public_key_fingerprint == spki_fingerprint(key.public_key())
    # The accepted proof is persisted on the row, not discarded.
    assert dev.pop_method == "dpop"
    assert dev.pop_proof == proof


def test_enrollment_service_rejects_bad_key(registry) -> None:
    service = EnrollmentService(registry)
    with pytest.raises(EnrollmentError):
        service.enroll(EnrollmentRequest(name="x", public_key="not a pem", dpop_proof="AAAA"))


def test_enrollment_service_rejects_non_p256_key(registry) -> None:
    service = EnrollmentService(registry)
    with pytest.raises(EnrollmentError):
        service.enroll(EnrollmentRequest(name="x", public_key=_rsa_public_pem(), dpop_proof="AAAA"))


def test_enrollment_service_validates_matching_csr(registry) -> None:
    key = _new_device_key()
    csr = _make_csr(key)  # capture: a re-generated CSR would carry a different signature.
    service = EnrollmentService(registry)
    dev = service.enroll(EnrollmentRequest(name="d", public_key=_spki_pem_of(key), csr=csr))
    assert dev.status == STATUS_PENDING
    assert dev.pop_method == "csr"
    assert dev.pop_proof == csr


def test_enrollment_service_rejects_mismatched_csr(registry) -> None:
    key, other = _new_device_key(), _new_device_key()
    service = EnrollmentService(registry)
    with pytest.raises(EnrollmentError):
        service.enroll(
            EnrollmentRequest(name="d", public_key=_spki_pem_of(key), csr=_make_csr(other))
        )


# --------------------------------------------------------------------------- #
# Proof of possession — bare key rejected, bad DPoP rejected (anti-squatting)
# --------------------------------------------------------------------------- #


def test_enrollment_rejects_bare_public_key(registry) -> None:
    # No csr, no dpop_proof -> possession is unproven -> refused (fingerprint-squatting).
    key = _new_device_key()
    service = EnrollmentService(registry)
    with pytest.raises(EnrollmentError, match="proof of possession"):
        service.enroll(EnrollmentRequest(name="d", public_key=_spki_pem_of(key)))


def test_enrollment_rejects_dpop_signed_by_wrong_key(registry) -> None:
    key, attacker = _new_device_key(), _new_device_key()
    service = EnrollmentService(registry)
    # A proof produced by a different key must not verify against public_key.
    with pytest.raises(EnrollmentError, match="dpop_proof"):
        service.enroll(
            EnrollmentRequest(
                name="d", public_key=_spki_pem_of(key), dpop_proof=_dpop_proof(attacker)
            )
        )


def test_enrollment_rejects_non_base64_dpop(registry) -> None:
    key = _new_device_key()
    service = EnrollmentService(registry)
    with pytest.raises(EnrollmentError, match="base64"):
        service.enroll(
            EnrollmentRequest(name="d", public_key=_spki_pem_of(key), dpop_proof="!!!not base64!!!")
        )


# --------------------------------------------------------------------------- #
# DoS caps — oversize fields (field-level) + pending-row cap (registry)
# --------------------------------------------------------------------------- #


def test_enrollment_request_rejects_oversize_public_key() -> None:
    # Field-level max_length: oversize input is rejected at validation, before the store.
    with pytest.raises(ValidationError):
        EnrollmentRequest(name="d", public_key="x" * (MAX_PUBLIC_KEY_LEN + 1), dpop_proof="AAAA")


def test_registry_pending_cap_refuses_new_but_allows_reenroll(store) -> None:
    registry = DeviceRegistry(store, max_pending_devices=2)
    keys = [_new_device_key() for _ in range(2)]
    for k in keys:
        registry.enroll_request(
            name="d", public_key_pem=_spki_pem_of(k),
            public_key_fingerprint=spki_fingerprint(k.public_key()),
        )
    # A NEW (third) enrollment is refused once the pending cap is reached.
    third = _new_device_key()
    with pytest.raises(PendingCapExceeded):
        registry.enroll_request(
            name="d", public_key_pem=_spki_pem_of(third),
            public_key_fingerprint=spki_fingerprint(third.public_key()),
        )
    # An idempotent re-POST of an already-pending key is NOT counted against the cap.
    again = registry.enroll_request(
        name="d", public_key_pem=_spki_pem_of(keys[0]),
        public_key_fingerprint=spki_fingerprint(keys[0].public_key()),
    )
    assert again.status == STATUS_PENDING
    assert registry.count_pending() == 2


def test_registry_pending_cap_frees_up_after_accept(store) -> None:
    registry = DeviceRegistry(store, max_pending_devices=1)
    k1, k2 = _new_device_key(), _new_device_key()
    d1 = registry.enroll_request(
        name="a", public_key_pem=_spki_pem_of(k1),
        public_key_fingerprint=spki_fingerprint(k1.public_key()),
    )
    # Accepting the pending device frees a slot, so a new enrollment succeeds.
    registry.accept(d1.id, cert_fingerprint="f")
    d2 = registry.enroll_request(
        name="b", public_key_pem=_spki_pem_of(k2),
        public_key_fingerprint=spki_fingerprint(k2.public_key()),
    )
    assert d2.status == STATUS_PENDING


# --------------------------------------------------------------------------- #
# Revocation is terminal; concurrent same-fingerprint enroll is idempotent
# --------------------------------------------------------------------------- #


def test_accept_of_revoked_device_is_rejected(registry) -> None:
    key = _new_device_key()
    dev = registry.enroll_request(
        name="d", public_key_pem=_spki_pem_of(key),
        public_key_fingerprint=spki_fingerprint(key.public_key()),
    )
    registry.revoke(dev.id)
    # Revocation is terminal: no silent revoke -> accept un-revocation.
    with pytest.raises(InvalidTransition):
        registry.accept(dev.id, cert_fingerprint="x")
    assert registry.get(dev.id).status == STATUS_REVOKED


def test_concurrent_same_fingerprint_enroll_is_idempotent(store, monkeypatch) -> None:
    # Simulate the race: the in-transaction existence check misses (stale read), so the
    # INSERT runs and trips the unique constraint. The IntegrityError must resolve to the
    # existing row, not surface a 500.
    registry = DeviceRegistry(store)
    key = _new_device_key()
    fp = spki_fingerprint(key.public_key())
    first = registry.enroll_request(
        name="first", public_key_pem=_spki_pem_of(key), public_key_fingerprint=fp
    )

    real = DeviceRegistry._by_fingerprint
    calls = {"n": 0}

    def flaky(session, f):
        calls["n"] += 1
        return None if calls["n"] == 1 else real(session, f)

    monkeypatch.setattr(DeviceRegistry, "_by_fingerprint", staticmethod(flaky))
    dup = registry.enroll_request(
        name="second", public_key_pem=_spki_pem_of(key), public_key_fingerprint=fp
    )
    assert dup.id == first.id
    assert len(registry.list()) == 1


# --------------------------------------------------------------------------- #
# POST /device/enroll — reachable without a client cert, only ever pending
# --------------------------------------------------------------------------- #


def _enroll_app(store, settings, **overrides):
    settings = settings.model_copy(update=overrides)
    pipeline = IngestPipeline(store)
    return create_app(pipeline=pipeline, settings=settings)


def test_enroll_endpoint_stores_pending_without_client_cert(store, settings) -> None:
    # mTLS ON, no X-Client-Cert header, non-loopback peer: /ingest would 401, but the
    # enroll intake is EXEMPT (bootstrap chicken-and-egg) and must succeed.
    app = _enroll_app(store, settings, require_mtls=True, trust_loopback_ingest=False)
    key = _new_device_key()
    with TestClient(app, client=("203.0.113.7", 40000)) as c:
        # Control: the mTLS-gated ingest endpoint rejects this same caller.
        assert c.post("/ingest/event", json={
            "event_id": "e1", "source": "github", "account_ref": "o",
            "kind": "github.issue", "summary": "s",
        }).status_code == 401

        r = c.post("/device/enroll", json=_enroll_payload(key, key_provenance="hardware"))
        assert r.status_code == 201
        body = r.json()
        assert body["status"] == STATUS_PENDING
        assert body["trusted"] is False
        assert body["accepted"] is False
        assert body["public_key_fingerprint"] == spki_fingerprint(key.public_key())

    # The row exists and is pending — never accepted by enrolling.
    dev = DeviceRegistry(store).by_fingerprint(spki_fingerprint(key.public_key()))
    assert dev is not None
    assert dev.status == STATUS_PENDING
    assert dev.cert_fingerprint is None


def test_enroll_endpoint_is_idempotent(store, settings) -> None:
    app = _enroll_app(store, settings, require_mtls=False)
    key = _new_device_key()
    payload = _enroll_payload(key)
    with TestClient(app) as c:
        id1 = c.post("/device/enroll", json=payload).json()["device_id"]
        id2 = c.post("/device/enroll", json=payload).json()["device_id"]
    assert id1 == id2
    assert len(DeviceRegistry(store).list()) == 1


def test_enroll_endpoint_rejects_bad_key(store, settings) -> None:
    app = _enroll_app(store, settings, require_mtls=False)
    with TestClient(app) as c:
        r = c.post(
            "/device/enroll", json={"name": "x", "public_key": "garbage", "dpop_proof": "AA"}
        )
    assert r.status_code == 422
    assert r.json()["error"] == "invalid_enrollment"


def test_enroll_endpoint_rejects_bare_public_key(store, settings) -> None:
    # No proof of possession -> 422 (fingerprint-squatting is refused).
    app = _enroll_app(store, settings, require_mtls=False)
    key = _new_device_key()
    with TestClient(app) as c:
        r = c.post("/device/enroll", json={"name": "x", "public_key": _spki_pem_of(key)})
    assert r.status_code == 422
    assert r.json()["error"] == "invalid_enrollment"


def test_enroll_endpoint_rejects_oversize_public_key(store, settings) -> None:
    # Field-level max_length -> FastAPI request-validation 422 (before the store).
    app = _enroll_app(store, settings, require_mtls=False)
    with TestClient(app) as c:
        r = c.post(
            "/device/enroll",
            json={"name": "x", "public_key": "x" * (MAX_PUBLIC_KEY_LEN + 1), "dpop_proof": "AA"},
        )
    assert r.status_code == 422


def test_enroll_endpoint_pending_cap_returns_429(store, settings) -> None:
    app = _enroll_app(store, settings, require_mtls=False, max_pending_devices=1)
    k1, k2 = _new_device_key(), _new_device_key()
    with TestClient(app) as c:
        assert c.post("/device/enroll", json=_enroll_payload(k1)).status_code == 201
        r = c.post("/device/enroll", json=_enroll_payload(k2))
        assert r.status_code == 429
        assert r.json()["error"] == "pending_capacity"


# --------------------------------------------------------------------------- #
# small helpers that need an import kept local
# --------------------------------------------------------------------------- #


def _rsa_public_pem() -> str:
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")


def _root_private_pem(vault) -> str:
    return vault.get(CA_VAULT_PROVIDER, CA_VAULT_ACCOUNT)["private_key_pem"]
