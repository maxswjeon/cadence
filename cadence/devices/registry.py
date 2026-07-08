"""The device registry — status-based trust state for enrolled devices.

:class:`DeviceRegistry` is the **data layer** for the mTLS trust fabric: it inserts
pending devices, looks them up by id or key fingerprint, and flips their trust status
(``pending`` → ``accepted`` / ``revoked``). These are pure data operations — the
operator CLI and the runtime verification middleware that *call* accept/revoke live in
B2 and are deliberately not built here.

Device rows carry public keys and fingerprints (security metadata), so they are written
straight through the local D1 session rather than :meth:`D1Store.write` — they never
pass the derived-content raw-boundary/replication path and never reach the Cloudflare
replica. ``status`` is the single source of truth for trust; there is no CRL.

Two abuse-resistance properties matter because the enrollment intake is un-authenticated
(see :mod:`cadence.devices.enrollment`):

* A **pending-row cap** (``settings.max_pending_devices``) bounds storage-amplification
  growth from anonymous callers — a NEW enrollment past the cap is refused
  (:class:`PendingCapExceeded`). Idempotent re-POSTs of a known key are never capped.
* Enrollment is **fingerprint-idempotent even under concurrency**: a racing second
  insert of the same key hits the unique constraint and is resolved to the existing row
  rather than surfacing a 500.
"""

from __future__ import annotations

from sqlalchemy.exc import IntegrityError

from cadence.config import get_settings
from cadence.stores.d1 import D1Store
from cadence.stores.models import Device, utcnow

#: The device trust states, in lifecycle order.
STATUS_PENDING = "pending"
STATUS_ACCEPTED = "accepted"
STATUS_REVOKED = "revoked"
VALID_STATUSES = frozenset({STATUS_PENDING, STATUS_ACCEPTED, STATUS_REVOKED})

#: Key-provenance vocabulary.
VALID_PROVENANCE = frozenset({"hardware", "software", "unknown"})


class PendingCapExceeded(Exception):
    """Too many devices are already ``pending``; a new enrollment is refused.

    Signals the un-authenticated intake to fail closed (HTTP 429) rather than let an
    anonymous caller grow the table without bound.
    """


class InvalidTransition(Exception):
    """A status transition is not allowed (e.g. accepting an already-revoked device)."""


class DeviceRegistry:
    """Status-based device registry over the local-canonical D1 store."""

    def __init__(self, d1: D1Store, *, max_pending_devices: int | None = None) -> None:
        self._d1 = d1
        # Fall back to the process settings so the plain ``DeviceRegistry(d1)`` call
        # still enforces the configured cap.
        self._max_pending = (
            max_pending_devices
            if max_pending_devices is not None
            else get_settings().max_pending_devices
        )

    # -- writes ------------------------------------------------------------- #

    def enroll_request(
        self,
        *,
        name: str,
        public_key_pem: str,
        public_key_fingerprint: str,
        key_provenance: str = "unknown",
        attestation_ref: str | None = None,
        pop_method: str | None = None,
        pop_proof: str | None = None,
    ) -> Device:
        """Insert a **pending** device, or return the existing row for a known key.

        Fingerprint-idempotent: re-enrolling the same public key returns the existing
        device unchanged rather than creating a duplicate (the fingerprint column is
        unique). Enrolling never confers trust — the row is ``pending`` until a human
        accepts it in B2.

        A NEW enrollment is refused with :class:`PendingCapExceeded` when the pending
        count is already at ``max_pending_devices``; an idempotent re-POST of an
        existing fingerprint is exempt from the cap (it adds no row).
        """
        if key_provenance not in VALID_PROVENANCE:
            key_provenance = "unknown"
        try:
            with self._d1.session() as session:
                existing = self._by_fingerprint(session, public_key_fingerprint)
                if existing is not None:
                    session.expunge(existing)
                    return existing
                # Only NEW rows count against the cap (re-POSTs returned above don't).
                pending_count = (
                    session.query(Device).filter(Device.status == STATUS_PENDING).count()
                )
                if pending_count >= self._max_pending:
                    raise PendingCapExceeded(
                        f"pending-device cap reached ({self._max_pending}); "
                        "accept or revoke pending devices before enrolling more"
                    )
                device = Device(
                    name=name,
                    public_key_pem=public_key_pem,
                    public_key_fingerprint=public_key_fingerprint,
                    status=STATUS_PENDING,
                    key_provenance=key_provenance,
                    attestation_ref=attestation_ref,
                    pop_method=pop_method,
                    pop_proof=pop_proof,
                    enrolled_at=utcnow(),
                )
                session.add(device)
                session.flush()
                session.expunge(device)
                return device
        except IntegrityError:
            # A concurrent enrollment of the *same* fingerprint won the unique-constraint
            # race. Resolve to the row that now exists instead of surfacing a 500 —
            # enrollment stays idempotent even under concurrency.
            existing = self.by_fingerprint(public_key_fingerprint)
            if existing is not None:
                return existing
            raise

    def accept(self, device_id: str, cert_fingerprint: str) -> Device:
        """Transition a device to ``accepted`` and record its issued cert fingerprint.

        Revocation is **terminal**: accepting an already-``revoked`` device raises
        :class:`InvalidTransition` (no silent revoke→accept un-revocation). Data op only;
        the human-driven operator flow that issues the cert and calls this lives in B2.
        """
        with self._d1.session() as session:
            device = self._get_or_raise(session, device_id)
            if device.status == STATUS_REVOKED:
                raise InvalidTransition(
                    f"device {device_id!r} is revoked; revocation is terminal and cannot "
                    "be accepted back into the trust fabric (re-enroll a fresh key instead)"
                )
            device.status = STATUS_ACCEPTED
            device.cert_fingerprint = cert_fingerprint
            device.decided_at = utcnow()
            session.flush()
            session.expunge(device)
            return device

    def revoke(self, device_id: str) -> Device:
        """Transition a device to ``revoked`` (registry-status-based revocation)."""
        with self._d1.session() as session:
            device = self._get_or_raise(session, device_id)
            device.status = STATUS_REVOKED
            device.decided_at = utcnow()
            session.flush()
            session.expunge(device)
            return device

    # -- reads -------------------------------------------------------------- #

    def get(self, device_id: str) -> Device | None:
        with self._d1.session() as session:
            device = session.get(Device, device_id)
            if device is not None:
                session.expunge(device)
            return device

    def by_fingerprint(self, public_key_fingerprint: str) -> Device | None:
        with self._d1.session() as session:
            device = self._by_fingerprint(session, public_key_fingerprint)
            if device is not None:
                session.expunge(device)
            return device

    def list(self, status: str | None = None) -> list[Device]:
        with self._d1.session() as session:
            query = session.query(Device)
            if status is not None:
                query = query.filter(Device.status == status)
            devices = query.order_by(Device.enrolled_at).all()
            for device in devices:
                session.expunge(device)
            return devices

    def count_pending(self) -> int:
        """Number of devices currently in the ``pending`` state (observability/tests)."""
        with self._d1.session() as session:
            return session.query(Device).filter(Device.status == STATUS_PENDING).count()

    # -- internals ---------------------------------------------------------- #

    @staticmethod
    def _by_fingerprint(session, public_key_fingerprint: str) -> Device | None:
        return (
            session.query(Device)
            .filter(Device.public_key_fingerprint == public_key_fingerprint)
            .one_or_none()
        )

    @staticmethod
    def _get_or_raise(session, device_id: str) -> Device:
        device = session.get(Device, device_id)
        if device is None:
            raise KeyError(f"no device with id {device_id!r}")
        return device


__all__ = [
    "DeviceRegistry",
    "PendingCapExceeded",
    "InvalidTransition",
    "STATUS_PENDING",
    "STATUS_ACCEPTED",
    "STATUS_REVOKED",
    "VALID_STATUSES",
    "VALID_PROVENANCE",
]
