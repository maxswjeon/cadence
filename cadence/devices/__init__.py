"""Cadence device trust fabric (M8 / B1).

* :mod:`cadence.devices.ca` — the private Cadence CA (EC P-256; vault-sealed root key).
* :mod:`cadence.devices.registry` — status-based device registry (pending/accepted/revoked).
* :mod:`cadence.devices.enrollment` — the un-authenticated TOFU enrollment intake.

Enrolling grants no trust: a device stays ``pending`` (and gets no client cert) until a
human explicitly accepts it. The operator CLI and the runtime mTLS verification
middleware that consume this layer are B2, not built here.
"""

from cadence.devices.ca import CadenceCA
from cadence.devices.enrollment import EnrollmentError, EnrollmentRequest, EnrollmentService
from cadence.devices.registry import DeviceRegistry

__all__ = [
    "CadenceCA",
    "DeviceRegistry",
    "EnrollmentRequest",
    "EnrollmentService",
    "EnrollmentError",
]
