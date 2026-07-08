"""``cadence device`` — the operator console for the mTLS device trust fabric.

This is the **human side** of enrollment: the un-authenticated intake
(:mod:`cadence.devices.enrollment`) only ever records a ``pending`` device, and a device
is granted no trust until an operator reviews it here and ``accept``s it. Accepting is
the single trust action — it mints a short-lived clientAuth cert from the device's stored
public key (:meth:`CadenceCA.issue_client_cert`), records the cert fingerprint on the row
(:meth:`DeviceRegistry.accept`), and prints the issued client cert PEM plus the CA cert
PEM (the pin) for the operator to deliver out of band.

Subcommands::

    cadence device requests                 # the pending enrollment queue
    cadence device list [--status ...]       # all devices, optionally filtered
    cadence device show <id>                 # full details of one device
    cadence device accept <id>               # issue a cert + flip pending -> accepted
    cadence device revoke <id>               # flip -> revoked (terminal)

Security note — the ``name`` and ``attestation_ref`` fields originate from the
**anonymous** enrollment intake and are attacker-controlled until a human accepts the
device. They are rendered to the operator's terminal, so every human-facing print routes
them through :func:`sanitize_for_terminal`, which escapes ASCII/C1 control characters and
ANSI/terminal escape sequences so a malicious enrollment name cannot inject escapes into
the operator console.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from datetime import datetime

from cryptography import x509

from cadence.adapters.vault import FileCredentialVault
from cadence.config import Settings, get_settings
from cadence.devices.ca import CadenceCA, cert_fingerprint
from cadence.devices.registry import (
    STATUS_ACCEPTED,
    STATUS_PENDING,
    STATUS_REVOKED,
    DeviceRegistry,
)
from cadence.stores.d1 import D1Store
from cadence.stores.models import Device

#: How many hex chars of a fingerprint to show in the compact list views.
_SHORT_FP_LEN = 12


# --------------------------------------------------------------------------- #
# Terminal-injection defense for anonymous, attacker-controlled fields
# --------------------------------------------------------------------------- #


def sanitize_for_terminal(text: str | None) -> str:
    """Neutralize control/escape sequences in operator-facing, untrusted text.

    The device ``name`` and ``attestation_ref`` come from the un-authenticated enrollment
    intake, so a hostile enroller could embed ANSI/terminal escape sequences (``\\x1b[…``),
    the 8-bit CSI (``\\x9b``), or other control characters to rewrite the operator's
    console. Each non-printable character is replaced with its escaped form (e.g. ESC
    becomes the literal text ``\\x1b``) so nothing the terminal would interpret survives.
    ``str.isprintable()`` is False for every C0/C1 control character (including ESC and
    CSI) while staying True for spaces and ordinary printable text.
    """
    if not text:
        return ""
    out: list[str] = []
    for ch in text:
        if ch.isprintable():
            out.append(ch)
        else:
            out.append(ch.encode("unicode_escape").decode("ascii"))
    return "".join(out)


# --------------------------------------------------------------------------- #
# Service wiring (same-host operator tool: local D1 + NAS vault)
# --------------------------------------------------------------------------- #


def _build_services(settings: Settings | None = None) -> tuple[DeviceRegistry, CadenceCA]:
    """Build the registry (over local-canonical D1) and CA (over the NAS vault)."""
    settings = settings or get_settings()
    registry = DeviceRegistry(D1Store(settings))
    ca = CadenceCA(FileCredentialVault(settings), settings)
    return registry, ca


# --------------------------------------------------------------------------- #
# Formatting helpers
# --------------------------------------------------------------------------- #


def _short(fingerprint: str) -> str:
    return fingerprint[:_SHORT_FP_LEN]


def _fmt_dt(value: datetime | None) -> str:
    return "-" if value is None else value.isoformat(timespec="seconds")


def _print_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> None:
    """Print a simple left-aligned, space-padded column table to stdout."""
    if not rows:
        print("(none)")
        return
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    print(fmt.format(*headers))
    for row in rows:
        print(fmt.format(*row))


def _device_row(device: Device) -> list[str]:
    return [
        device.id,
        sanitize_for_terminal(device.name),
        _short(device.public_key_fingerprint),
        device.key_provenance,
        device.pop_method or "-",
        _fmt_dt(device.enrolled_at),
    ]


def _print_device_table(devices: Sequence[Device]) -> None:
    _print_table(
        ("ID", "NAME", "FINGERPRINT", "PROVENANCE", "POP", "ENROLLED"),
        [_device_row(d) for d in devices],
    )


def _print_device_details(device: Device) -> None:
    fields = [
        ("id", device.id),
        ("name", sanitize_for_terminal(device.name)),
        ("status", device.status),
        ("key_provenance", device.key_provenance),
        ("public_key_fingerprint", device.public_key_fingerprint),
        ("pop_method", device.pop_method or "-"),
        ("cert_fingerprint", device.cert_fingerprint or "-"),
        ("attestation_ref", sanitize_for_terminal(device.attestation_ref) or "-"),
        ("enrolled_at", _fmt_dt(device.enrolled_at)),
        ("decided_at", _fmt_dt(device.decided_at)),
    ]
    label_width = max(len(label) for label, _ in fields)
    for label, value in fields:
        print(f"{label + ':':<{label_width + 1}} {value}")
    print()
    print(device.public_key_pem.strip())


# --------------------------------------------------------------------------- #
# Subcommands
# --------------------------------------------------------------------------- #


def _cmd_requests(registry: DeviceRegistry) -> int:
    """The enrollment queue: devices awaiting an operator decision."""
    _print_device_table(registry.list(status=STATUS_PENDING))
    return 0


def _cmd_list(registry: DeviceRegistry, status: str | None) -> int:
    _print_device_table(registry.list(status=status))
    return 0


def _cmd_show(registry: DeviceRegistry, device_id: str) -> int:
    device = registry.get(device_id)
    if device is None:
        print(f"cadence device: no device with id {device_id!r}", file=sys.stderr)
        return 1
    _print_device_details(device)
    return 0


def _cmd_accept(registry: DeviceRegistry, ca: CadenceCA, device_id: str) -> int:
    """The trust action: issue a clientAuth cert and flip pending -> accepted."""
    device = registry.get(device_id)
    if device is None:
        print(f"cadence device: no device with id {device_id!r}", file=sys.stderr)
        return 1
    if device.status != STATUS_PENDING:
        print(
            f"cadence device: device {device_id!r} is {device.status}, not pending; only a "
            "pending device can be accepted (revocation is terminal)",
            file=sys.stderr,
        )
        return 1

    # Issue from the device's stored public key. The subject CN is the (trusted) device
    # id, never the attacker-controlled name.
    cert_pem = ca.issue_client_cert(device.public_key_pem, device.id)
    fingerprint = cert_fingerprint(x509.load_pem_x509_certificate(cert_pem.encode("ascii")))
    registry.accept(device.id, fingerprint)

    name = sanitize_for_terminal(device.name)
    print(f"Accepted device {device.id} ({name}).")
    print(f"Issued clientAuth certificate (fingerprint {fingerprint}).")
    print()
    print("Deliver BOTH of the following to the device:")
    print()
    print("----- CLIENT CERTIFICATE (this device's identity) -----")
    print(cert_pem.strip())
    print("----- CA CERTIFICATE (pin / trust anchor) -----")
    print(ca.ca_cert_pem().strip())
    return 0


def _cmd_revoke(registry: DeviceRegistry, device_id: str) -> int:
    device = registry.get(device_id)
    if device is None:
        print(f"cadence device: no device with id {device_id!r}", file=sys.stderr)
        return 1
    registry.revoke(device_id)
    print(f"Revoked device {device.id} ({sanitize_for_terminal(device.name)}).")
    return 0


# --------------------------------------------------------------------------- #
# Argument parsing + dispatch
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cadence device",
        description="Operator console for the mTLS device trust fabric (registry + CA).",
    )
    sub = parser.add_subparsers(dest="subcommand", metavar="<subcommand>")

    sub.add_parser("requests", help="List PENDING devices (the enrollment queue).")

    p_list = sub.add_parser("list", help="List all devices, optionally filtered by status.")
    p_list.add_argument(
        "--status",
        choices=[STATUS_PENDING, STATUS_ACCEPTED, STATUS_REVOKED],
        default=None,
        help="Only show devices in this trust state.",
    )

    p_show = sub.add_parser("show", help="Show full details of one device.")
    p_show.add_argument("device_id", help="Device id.")

    p_accept = sub.add_parser(
        "accept", help="Issue a client cert and accept a pending device into the trust fabric."
    )
    p_accept.add_argument("device_id", help="Device id (must be pending).")

    p_revoke = sub.add_parser("revoke", help="Revoke a device (terminal).")
    p_revoke.add_argument("device_id", help="Device id.")

    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    registry: DeviceRegistry | None = None,
    ca: CadenceCA | None = None,
) -> int:
    """Run ``cadence device``. ``registry``/``ca`` may be injected (tests); otherwise they
    are built from the process settings (local D1 + NAS vault)."""
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.subcommand is None:
        parser.print_help(sys.stderr)
        return 2

    if registry is None or ca is None:
        built_registry, built_ca = _build_services()
        registry = registry or built_registry
        ca = ca or built_ca

    if args.subcommand == "requests":
        return _cmd_requests(registry)
    if args.subcommand == "list":
        return _cmd_list(registry, args.status)
    if args.subcommand == "show":
        return _cmd_show(registry, args.device_id)
    if args.subcommand == "accept":
        return _cmd_accept(registry, ca, args.device_id)
    if args.subcommand == "revoke":
        return _cmd_revoke(registry, args.device_id)

    parser.print_help(sys.stderr)  # pragma: no cover — argparse rejects unknown subcommands
    return 2


__all__ = ["main", "build_parser", "sanitize_for_terminal"]


if __name__ == "__main__":  # pragma: no cover — module entrypoint
    raise SystemExit(main())
