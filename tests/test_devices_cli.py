"""Tests for the ``cadence device`` operator CLI (M8/B2).

Each subcommand runs against an in-memory D1 registry + a temp-vault CA (injected into
``main``), so nothing touches process settings or the real NAS. The security-critical
assertion: a terminal-escape/control-char embedded in an anonymous device ``name`` is
neutralized in every operator-facing print (the raw escape is never emitted).
"""

from __future__ import annotations

import pytest
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID

from cadence.adapters.vault import FileCredentialVault
from cadence.devices.ca import CadenceCA, cert_fingerprint, spki_fingerprint, spki_pem
from cadence.devices.cli import main, sanitize_for_terminal
from cadence.devices.registry import (
    STATUS_ACCEPTED,
    STATUS_PENDING,
    STATUS_REVOKED,
    DeviceRegistry,
)

# --------------------------------------------------------------------------- #
# fixtures + helpers
# --------------------------------------------------------------------------- #


@pytest.fixture
def registry(store) -> DeviceRegistry:
    return DeviceRegistry(store)


@pytest.fixture
def ca(settings) -> CadenceCA:
    return CadenceCA(FileCredentialVault(settings), settings)


def _new_key() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


def _enroll(registry: DeviceRegistry, name: str = "phone", **extra):
    key = _new_key()
    return key, registry.enroll_request(
        name=name,
        public_key_pem=spki_pem(key.public_key()),
        public_key_fingerprint=spki_fingerprint(key.public_key()),
        **extra,
    )


def _run(registry, ca, *argv) -> int:
    return main(list(argv), registry=registry, ca=ca)


# --------------------------------------------------------------------------- #
# requests / list / show
# --------------------------------------------------------------------------- #


def test_requests_lists_only_pending(registry, ca, capsys) -> None:
    _key, pending = _enroll(registry, name="pending-phone")
    _key2, accepted = _enroll(registry, name="accepted-laptop")
    registry.accept(accepted.id, cert_fingerprint="f0")

    assert _run(registry, ca, "requests") == 0
    out = capsys.readouterr().out
    assert pending.id in out
    assert "pending-phone" in out
    # An already-accepted device is not part of the enrollment queue.
    assert accepted.id not in out
    assert "accepted-laptop" not in out


def test_list_all_and_filtered(registry, ca, capsys) -> None:
    _k, pending = _enroll(registry, name="p")
    _k2, accepted = _enroll(registry, name="a")
    registry.accept(accepted.id, cert_fingerprint="f0")

    assert _run(registry, ca, "list") == 0
    out = capsys.readouterr().out
    assert pending.id in out and accepted.id in out

    assert _run(registry, ca, "list", "--status", STATUS_ACCEPTED) == 0
    out = capsys.readouterr().out
    assert accepted.id in out and pending.id not in out


def test_show_full_details(registry, ca, capsys) -> None:
    key, device = _enroll(registry, name="workstation", key_provenance="hardware")
    assert _run(registry, ca, "show", device.id) == 0
    out = capsys.readouterr().out
    assert device.id in out
    assert "workstation" in out
    assert STATUS_PENDING in out
    assert device.public_key_fingerprint in out
    # The public key PEM is shown.
    assert "BEGIN PUBLIC KEY" in out


def test_show_unknown_id_errors(registry, ca, capsys) -> None:
    assert _run(registry, ca, "show", "nope") == 1
    assert "no device" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# accept — the trust action
# --------------------------------------------------------------------------- #


def test_accept_issues_ca_verifiable_cert_and_flips_status(registry, ca, capsys) -> None:
    key, device = _enroll(registry, name="to-accept")
    assert _run(registry, ca, "accept", device.id) == 0
    out = capsys.readouterr().out

    # Both the client cert and the CA pin are printed.
    assert "CLIENT CERTIFICATE" in out and "CA CERTIFICATE" in out
    assert out.count("BEGIN CERTIFICATE") == 2

    # The printed client cert is a real, CA-verifiable, clientAuth leaf for this key.
    leaf_pem = "-----BEGIN CERTIFICATE-----" + out.split("-----BEGIN CERTIFICATE-----", 1)[1]
    leaf_pem = leaf_pem.split("-----END CERTIFICATE-----", 1)[0] + "-----END CERTIFICATE-----\n"
    leaf = x509.load_pem_x509_certificate(leaf_pem.encode("ascii"))
    ca_cert = x509.load_pem_x509_certificate(ca.ca_cert_pem().encode("ascii"))
    ca_cert.public_key().verify(
        leaf.signature, leaf.tbs_certificate_bytes, ec.ECDSA(leaf.signature_hash_algorithm)
    )
    assert leaf.issuer == ca_cert.subject
    eku = leaf.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    assert ExtendedKeyUsageOID.CLIENT_AUTH in eku
    # The leaf carries the device's own key.
    assert spki_fingerprint(leaf.public_key()) == spki_fingerprint(key.public_key())

    # Status flipped to accepted with the issued cert's fingerprint recorded.
    stored = registry.get(device.id)
    assert stored.status == STATUS_ACCEPTED
    assert stored.cert_fingerprint == cert_fingerprint(leaf)
    assert stored.decided_at is not None


def test_accept_refuses_non_pending_device(registry, ca, capsys) -> None:
    _k, device = _enroll(registry, name="already")
    registry.accept(device.id, cert_fingerprint="f0")
    # Accepting an already-accepted device is refused (non-zero) and does not re-issue.
    assert _run(registry, ca, "accept", device.id) == 1
    err = capsys.readouterr().err
    assert "not pending" in err
    assert registry.get(device.id).cert_fingerprint == "f0"


def test_accept_refuses_revoked_device(registry, ca, capsys) -> None:
    _k, device = _enroll(registry, name="gone")
    registry.revoke(device.id)
    assert _run(registry, ca, "accept", device.id) == 1
    assert "not pending" in capsys.readouterr().err
    assert registry.get(device.id).status == STATUS_REVOKED


def test_accept_unknown_id_errors(registry, ca, capsys) -> None:
    assert _run(registry, ca, "accept", "nope") == 1
    assert "no device" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# revoke
# --------------------------------------------------------------------------- #


def test_revoke_flips_status(registry, ca, capsys) -> None:
    _k, device = _enroll(registry, name="revoke-me")
    assert _run(registry, ca, "revoke", device.id) == 0
    assert "Revoked" in capsys.readouterr().out
    assert registry.get(device.id).status == STATUS_REVOKED


def test_revoke_unknown_id_errors(registry, ca, capsys) -> None:
    assert _run(registry, ca, "revoke", "nope") == 1
    assert "no device" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# no subcommand -> usage, non-zero
# --------------------------------------------------------------------------- #


def test_no_subcommand_prints_help(registry, ca, capsys) -> None:
    assert _run(registry, ca) == 2
    assert "subcommand" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# SECURITY — terminal-escape/control-char in an anonymous name is neutralized
# --------------------------------------------------------------------------- #

# A hostile enroller's name: an ANSI SGR reset + a cursor-clear + the 8-bit CSI.
_HOSTILE_NAME = "evil\x1b[2J\x1b[31mPWNED\x9b0m"


def test_sanitize_neutralizes_control_and_escape_sequences() -> None:
    cleaned = sanitize_for_terminal(_HOSTILE_NAME)
    # No raw escape / CSI bytes survive; they are rendered as their escaped text form.
    assert "\x1b" not in cleaned
    assert "\x9b" not in cleaned
    assert "\\x1b" in cleaned
    # Printable content is preserved.
    assert "evil" in cleaned and "PWNED" in cleaned


@pytest.mark.parametrize("subcommand", ["requests", "list"])
def test_hostile_name_is_neutralized_in_list_views(registry, ca, capsys, subcommand) -> None:
    _enroll(registry, name=_HOSTILE_NAME)
    assert _run(registry, ca, subcommand) == 0
    out = capsys.readouterr().out
    assert "\x1b" not in out
    assert "\x9b" not in out


def test_hostile_name_is_neutralized_in_show(registry, ca, capsys) -> None:
    _k, device = _enroll(registry, name=_HOSTILE_NAME)
    assert _run(registry, ca, "show", device.id) == 0
    out = capsys.readouterr().out
    assert "\x1b" not in out and "\x9b" not in out


def test_hostile_name_is_neutralized_in_accept_and_revoke(registry, ca, capsys) -> None:
    _k, device = _enroll(registry, name=_HOSTILE_NAME)
    assert _run(registry, ca, "accept", device.id) == 0
    assert "\x1b" not in capsys.readouterr().out

    _k2, device2 = _enroll(registry, name=_HOSTILE_NAME)
    assert _run(registry, ca, "revoke", device2.id) == 0
    assert "\x1b" not in capsys.readouterr().out


def test_hostile_attestation_ref_is_neutralized_in_show(registry, ca, capsys) -> None:
    _k, device = _enroll(registry, name="safe", attestation_ref="ref\x1b[31mX\x9b0m")
    assert _run(registry, ca, "show", device.id) == 0
    out = capsys.readouterr().out
    assert "\x1b" not in out and "\x9b" not in out
