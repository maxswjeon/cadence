"""Tests for the adapter framework: Event schema, ABC/registry, and credential vault."""

from __future__ import annotations

import base64
import json
import os

import pytest
from pydantic import ValidationError

from cadence.adapters.base import (
    AcquisitionTier,
    Adapter,
    AdapterRegistry,
    Event,
)
from cadence.adapters.vault import FileCredentialVault
from cadence.config import DEFAULT_VAULT_MASTER_KEY, Settings
from cadence.obs.alarms import get_alarm_sink
from cadence.stores.raw_boundary import PayloadClassifier, RawBoundaryViolation


class DummyAdapter(Adapter):
    provider = "dummy"
    acquisition_tier = AcquisitionTier.OFFICIAL_API

    def fetch(self):
        return [{"id": "1", "title": "First"}, {"id": "2", "title": "Second"}]

    def normalize(self, raw):
        return Event(
            event_id=raw["id"],
            source=self.provider,
            account_ref=self.account_ref,
            kind="dummy.item",
            summary=f"item: {raw['title']}",
        )


def test_emit_yields_tagged_events() -> None:
    adapter = DummyAdapter("acct-1")
    events = list(adapter.emit())
    assert len(events) == 2
    for ev in events:
        assert ev.acquisition_tier is AcquisitionTier.OFFICIAL_API
        assert ev.dedupe_id  # filled by emit()
        assert ev.account_ref == "acct-1"


def test_registry_creates_per_account_instances() -> None:
    reg = AdapterRegistry()
    reg.register(DummyAdapter)
    assert reg.providers() == ["dummy"]
    a1 = reg.create("dummy", "acct-1")
    a2 = reg.create("dummy", "acct-2")
    assert a1.account_ref == "acct-1" and a2.account_ref == "acct-2"
    assert a1 is not a2


def test_registry_rejects_abstract_provider() -> None:
    reg = AdapterRegistry()
    with pytest.raises(ValueError):
        reg.register(Adapter)  # type: ignore[arg-type]


def test_event_dedupe_id_is_deterministic() -> None:
    e1 = Event(event_id="x", source="s", account_ref="a", kind="k").with_dedupe_id()
    e2 = Event(event_id="x", source="s", account_ref="a", kind="k").with_dedupe_id()
    assert e1.dedupe_id == e2.dedupe_id


def test_event_forbids_extra_fields() -> None:
    with pytest.raises(ValidationError):
        Event(event_id="x", source="s", account_ref="a", kind="k", raw_body="verbatim!")


# --- credential vault ------------------------------------------------------- #


def test_vault_store_get_revoke_roundtrip(settings) -> None:
    vault = FileCredentialVault(settings)
    vault.store("github", "octocat", {"token": "ghp_secret"})
    assert vault.get("github", "octocat") == {"token": "ghp_secret"}
    assert ("github", "octocat") in vault.list_accounts()
    vault.revoke("github", "octocat")
    with pytest.raises(KeyError):
        vault.get("github", "octocat")
    assert ("github", "octocat") not in vault.list_accounts()


def test_vault_access_fires_alarm(settings) -> None:
    vault = FileCredentialVault(settings)
    vault.store("p", "a", {"k": "v"})
    vault.get("p", "a")
    assert get_alarm_sink().count("credential_vault_access") >= 2


def test_vault_encrypts_at_rest(settings) -> None:
    vault = FileCredentialVault(settings)
    vault.store("p", "a", {"token": "PLAINTEXT_SECRET"})
    on_disk = list(settings.vault_dir.glob("*.cred"))[0].read_bytes()
    assert b"PLAINTEXT_SECRET" not in on_disk


def test_vault_refuses_cloud_location(settings) -> None:
    """Credential vault must never resolve under a cloud tier (D1/R2)."""
    with pytest.raises(ValueError):
        FileCredentialVault(settings, base_dir=settings.r2_dir / "r2_blobs")


def test_vault_uses_v2_aes_gcm_envelope(settings) -> None:
    """Records are sealed as the versioned AES-256-GCM envelope, not raw XOR bytes."""
    vault = FileCredentialVault(settings)
    vault.store("p", "a", {"token": "PLAINTEXT_SECRET"})
    on_disk = list(settings.vault_dir.glob("*.cred"))[0].read_bytes()
    envelope = json.loads(on_disk)
    assert envelope["v"] == 2
    assert envelope["alg"] == "AES-256-GCM"
    assert base64.b64decode(envelope["nonce"])  # decodable, non-empty
    assert b"PLAINTEXT_SECRET" not in base64.b64decode(envelope["ct"])


def test_vault_tampered_ciphertext_raises(settings) -> None:
    """Flipping a byte of the stored ciphertext makes get() raise, never return garbage."""
    vault = FileCredentialVault(settings)
    vault.store("p", "a", {"token": "s3cr3t"})
    cred = list(settings.vault_dir.glob("*.cred"))[0]
    envelope = json.loads(cred.read_bytes())
    ct = bytearray(base64.b64decode(envelope["ct"]))
    ct[0] ^= 0x01  # flip one bit of the ciphertext/tag
    envelope["ct"] = base64.b64encode(bytes(ct)).decode("ascii")
    cred.write_bytes(json.dumps(envelope).encode("utf-8"))
    with pytest.raises(ValueError):
        vault.get("p", "a")


def test_vault_aad_binds_ciphertext_to_slot(settings) -> None:
    """A ciphertext sealed for (A, X) cannot be opened as (B, Y) — the slot is AAD."""
    vault = FileCredentialVault(settings)
    vault.store("provider_a", "ref_x", {"token": "s"})
    src = vault._path("provider_a", "ref_x")
    dst = vault._path("provider_b", "ref_y")
    # Move the sealed blob into a different slot's file; decrypt must reject the AAD.
    dst.write_bytes(src.read_bytes())
    with pytest.raises(ValueError):
        vault.get("provider_b", "ref_y")


def test_vault_prod_default_key_refused_at_startup(tmp_path) -> None:
    """env=prod + the default master key must refuse to construct the vault."""
    prod = Settings(
        data_dir=tmp_path / "data",
        d1_path=tmp_path / "d1.sqlite",
        nas_dir=tmp_path / "nas",
        r2_dir=tmp_path / "r2",
        vault_dir=tmp_path / "vault",
        vault_master_key=DEFAULT_VAULT_MASTER_KEY,
        env="prod",
    )
    with pytest.raises(ValueError, match="CADENCE_VAULT_MASTER_KEY"):
        FileCredentialVault(prod)


def test_vault_rejects_legacy_v1_record(settings) -> None:
    """A legacy/unrecognized (non-v2) record is rejected, never silently accepted.

    The v1 stub authenticated only salt+ct, not the slot, so honoring it would reopen a
    slot-relocation downgrade. Any blob that is not a v2 AES-256-GCM envelope must fail.
    """
    vault = FileCredentialVault(settings)
    # Write a plausible pre-v2 stub record (raw salt+mac+ct bytes) into a slot's file.
    legacy_blob = b"\x8f" + os.urandom(63)
    vault._atomic_write(vault._path("github", "octocat"), legacy_blob)
    # Sanity: it really is not a v2 JSON envelope.
    assert vault._parse_v2_envelope(legacy_blob) is None
    with pytest.raises(ValueError):
        vault.get("github", "octocat")


def test_vault_aad_encoding_is_injective(settings) -> None:
    """The slot AAD must not collide across different (provider, account_ref) splits.

    A plain ``f"{provider}:{account_ref}"`` join maps ("a", "b:c") and ("a:b", "c") to the
    same AAD; the injective JSON encoding keeps them distinct.
    """
    vault = FileCredentialVault(settings)
    assert vault._aad("a", "b:c") != vault._aad("a:b", "c")
    # End-to-end: a secret stored at one split cannot be read at a colliding split.
    vault.store("a", "b:c", {"token": "s"})
    src = vault._path("a", "b:c")
    dst = vault._path("a:b", "c")
    dst.write_bytes(src.read_bytes())
    with pytest.raises(ValueError):
        vault.get("a:b", "c")


def test_credential_fields_are_blocked_from_d1() -> None:
    """A credential field can never enter D1 — the raw boundary rejects it."""
    classifier = PayloadClassifier()
    for field in ("token", "password", "secret", "credential", "api_token"):
        with pytest.raises(RawBoundaryViolation):
            classifier.enforce({field: "value"}, tier="D1")
