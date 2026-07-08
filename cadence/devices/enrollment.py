"""Enrollment intake — the un-authenticated, TOFU entry point to the trust fabric.

This is the chicken-and-egg boundary of mTLS: an un-enrolled device has no client
certificate yet, so the enrollment endpoint **cannot** be behind the mTLS gate. The
intake is therefore deliberately reachable without a cert — but it is also deliberately
**powerless**: it validates the submitted key, computes its fingerprint, and inserts a
single ``pending`` :class:`~cadence.stores.models.Device` row. Nothing is trusted by
enrolling.

Proof of possession (anti-squatting)
------------------------------------
An enrollment MUST prove it controls the private key for the public key it submits —
otherwise anyone could "squat" a victim's public key (grab its fingerprint/pending slot
before the real device enrolls). A **bare public-key enrollment is rejected**. The caller
must supply one of:

* ``csr`` — a PKCS#10 CSR self-signed by the device key (the signature is verified and
  its public key must match ``public_key``); or
* ``dpop_proof`` — base64 of the raw ECDSA-P256/SHA-256 **DER signature by the device
  private key over the DER SPKI of its own public key** (verified against ``public_key``).

The accepted proof is persisted on the pending row (``pop_method`` + ``pop_proof``) for
the B2 human-review audit trail — it is not discarded after verification. (A
challenge/nonce for replay-freshness is a later hardening; a static self-proof is
sufficient to stop squatting because it is bound to that specific public key.)

Trust model — **Trust On First Use + explicit human accept**:

* The device POSTs its public key + a PoP (and optional attestation).
* We record it as ``pending``. No certificate is issued, no access is granted.
* A human operator later reviews the pending device out of band and ``accept``s it (the
  operator CLI + the cert issuance live in B2). Only then is a client cert minted.

DoS posture: all fields are length-capped (below) and the registry enforces a
pending-row cap, because this endpoint is anonymous. Fingerprint idempotency keeps
retries/rate-limited clients cheap. Only references are stored — never an inlined
attestation blob.

NOTE for B2: the operator console must sanitize the anonymous, attacker-controlled
``name`` and ``attestation`` reference before display (terminal-escape / XSS) — they are
stored verbatim here and are untrusted until a human accepts the device.
"""

from __future__ import annotations

import base64
from typing import Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from pydantic import BaseModel, Field

from cadence.devices.ca import load_public_key_pem, spki_der, spki_fingerprint, spki_pem
from cadence.devices.registry import DeviceRegistry
from cadence.stores.models import Device

#: Length caps on the anonymous, un-authenticated enrollment payload. These bound the
#: storage-amplification an anonymous caller can drive per request (a P-256 SPKI PEM is
#: ~180 B; a CSR ~250 B; an Android key-attestation chain is a few KB).
MAX_PUBLIC_KEY_LEN = 2000
MAX_CSR_LEN = 4000
MAX_DPOP_LEN = 4096
MAX_ATTESTATION_LEN = 16384


class EnrollmentError(Exception):
    """The submitted enrollment payload is invalid (bad key, bad CSR, missing/bad PoP)."""


class EnrollmentRequest(BaseModel):
    """A device's self-submitted enrollment payload.

    ``public_key`` is the device key SPKI in PEM. A proof of possession is required:
    ``csr`` (self-signed PKCS#10) or ``dpop_proof`` (see the module docstring).
    ``attestation`` is an optional opaque reference carried through for the B2 human-review
    step — never inlined as a raw blob into D1. All fields are length-capped: oversize
    input is rejected by validation (HTTP 422) before it can touch the store.
    """

    model_config = {"extra": "forbid"}

    name: str = Field(min_length=1, max_length=255, description="Human label for the device.")
    public_key: str = Field(
        max_length=MAX_PUBLIC_KEY_LEN, description="Device public key, SPKI PEM (EC P-256)."
    )
    csr: str | None = Field(
        default=None, max_length=MAX_CSR_LEN, description="PKCS#10 CSR (PEM); a proof of possession"
    )
    key_provenance: Literal["hardware", "software", "unknown"] = Field(
        default="unknown", description="Where the private key lives (hardware keystore vs software)"
    )
    attestation: str | None = Field(
        default=None,
        max_length=MAX_ATTESTATION_LEN,
        description="Opaque NAS pointer / attestation reference (not inlined into D1).",
    )
    dpop_proof: str | None = Field(
        default=None,
        max_length=MAX_DPOP_LEN,
        description="base64 ECDSA-P256/SHA-256 signature by the device key over its DER SPKI.",
    )


class EnrollmentService:
    """Validates an :class:`EnrollmentRequest` and records a pending device.

    This service **never** trusts or accepts a device; it only ever produces a
    ``pending`` row (see the module docstring's PoP + TOFU + human-accept model).
    """

    def __init__(self, registry: DeviceRegistry) -> None:
        self._registry = registry

    def enroll(self, request: EnrollmentRequest) -> Device:
        """Validate the payload (incl. proof of possession) and insert a pending device.

        Raises :class:`EnrollmentError` on an invalid key or a missing/invalid proof of
        possession. May raise :class:`~cadence.devices.registry.PendingCapExceeded` when
        the pending-device cap is reached.
        """
        try:
            public_key = load_public_key_pem(request.public_key)
        except ValueError as exc:
            raise EnrollmentError(f"invalid public_key: {exc}") from exc

        pop_method, pop_proof = self._verify_proof_of_possession(request, public_key)

        # Re-serialize from the parsed key so a normalized, canonical SPKI PEM is stored
        # regardless of the exact PEM formatting the device submitted.
        return self._registry.enroll_request(
            name=request.name,
            public_key_pem=spki_pem(public_key),
            public_key_fingerprint=spki_fingerprint(public_key),
            key_provenance=request.key_provenance,
            attestation_ref=request.attestation,
            pop_method=pop_method,
            pop_proof=pop_proof,
        )

    def _verify_proof_of_possession(
        self, request: EnrollmentRequest, public_key: ec.EllipticCurvePublicKey
    ) -> tuple[str, str]:
        """Return ``(method, proof)`` after verifying possession, else raise.

        Requires a matching CSR or a valid DPoP-style self-signature. A bare public key
        (no proof) is rejected to stop fingerprint-squatting.
        """
        if request.csr is not None:
            self._validate_csr_matches(request.csr, public_key)
            return "csr", request.csr
        if request.dpop_proof is not None:
            self._verify_dpop(request.dpop_proof, public_key)
            return "dpop", request.dpop_proof
        raise EnrollmentError(
            "proof of possession required: supply a csr or a dpop_proof signed by the "
            "enrolling key (a bare public key cannot be enrolled)"
        )

    @staticmethod
    def _validate_csr_matches(csr_pem: str, public_key: ec.EllipticCurvePublicKey) -> None:
        from cryptography import x509

        try:
            csr = x509.load_pem_x509_csr(csr_pem.encode("ascii"))
        except ValueError as exc:
            raise EnrollmentError(f"invalid csr: {exc}") from exc
        if not csr.is_signature_valid:
            raise EnrollmentError("csr signature is invalid")
        if spki_fingerprint(csr.public_key()) != spki_fingerprint(public_key):
            raise EnrollmentError("csr public key does not match public_key")

    @staticmethod
    def _verify_dpop(dpop_proof: str, public_key: ec.EllipticCurvePublicKey) -> None:
        """Verify a DPoP-style self-signature: base64 DER ECDSA sig over the DER SPKI."""
        try:
            signature = base64.b64decode(dpop_proof, validate=True)
        except (ValueError, TypeError) as exc:
            raise EnrollmentError(f"dpop_proof is not valid base64: {exc}") from exc
        try:
            public_key.verify(signature, spki_der(public_key), ec.ECDSA(hashes.SHA256()))
        except InvalidSignature as exc:
            raise EnrollmentError(
                "dpop_proof signature does not verify against public_key"
            ) from exc


__all__ = [
    "EnrollmentRequest",
    "EnrollmentService",
    "EnrollmentError",
    "MAX_PUBLIC_KEY_LEN",
    "MAX_CSR_LEN",
    "MAX_DPOP_LEN",
    "MAX_ATTESTATION_LEN",
]
