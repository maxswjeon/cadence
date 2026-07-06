"""Tests for the adapter framework: Event schema, ABC/registry, and credential vault."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from cadence.adapters.base import (
    AcquisitionTier,
    Adapter,
    AdapterRegistry,
    Event,
)
from cadence.adapters.vault import FileCredentialVault
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


def test_credential_fields_are_blocked_from_d1() -> None:
    """A credential field can never enter D1 — the raw boundary rejects it."""
    classifier = PayloadClassifier()
    for field in ("token", "password", "secret", "credential", "api_token"):
        with pytest.raises(RawBoundaryViolation):
            classifier.enforce({field: "value"}, tier="D1")
