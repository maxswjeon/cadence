"""Cadence device trust fabric (M8 / B1).

* :mod:`cadence.devices.ca` — the private Cadence CA (EC P-256; vault-sealed root key).
* :mod:`cadence.devices.registry` — status-based device registry (pending/accepted/revoked).
* :mod:`cadence.devices.enrollment` — the un-authenticated TOFU enrollment intake.
* :mod:`cadence.devices.verify` — the runtime mTLS client-cert verifier (B2).

Enrolling grants no trust: a device stays ``pending`` (and gets no client cert) until a
human explicitly accepts it. The runtime mTLS verification middleware
(:class:`~cadence.devices.verify.DeviceVerifier`) that enforces trust at request time is
B2; the operator CLI that accepts/revokes devices is a sibling B2 task.
"""

from cadence.devices.ca import CadenceCA
from cadence.devices.enrollment import EnrollmentError, EnrollmentRequest, EnrollmentService
from cadence.devices.registry import DeviceRegistry
from cadence.devices.verify import DeviceIdentity, DeviceVerificationError, DeviceVerifier

__all__ = [
    "CadenceCA",
    "DeviceRegistry",
    "EnrollmentRequest",
    "EnrollmentService",
    "EnrollmentError",
    "DeviceVerifier",
    "DeviceVerificationError",
    "DeviceIdentity",
]
