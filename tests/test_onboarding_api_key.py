"""Tests for the API-key onboarding path (the fully-supported default)."""

from __future__ import annotations

import pytest

from cadence.adapters.vault import FileCredentialVault
from cadence.onboarding.api_key import store_api_key, validate_api_key


def test_validate_api_key_accepts_plausible_key() -> None:
    validate_api_key("sk-abcdefghijklmnop")  # must not raise


@pytest.mark.parametrize("bad", ["", "not-a-key", "sk-", "sk-short", "ghp_wrongprefix1234"])
def test_validate_api_key_rejects_implausible_keys(bad: str) -> None:
    with pytest.raises(ValueError, match="OpenAI API key"):
        validate_api_key(bad)


def test_store_api_key_persists_to_vault(settings) -> None:
    vault = FileCredentialVault(settings)
    store_api_key("sk-abcdefghijklmnop", settings=settings, vault=vault)
    stored = vault.get("openai_api", "default")
    assert stored == {"api_key": "sk-abcdefghijklmnop"}


def test_store_api_key_rejects_implausible_key_before_touching_vault(settings) -> None:
    vault = FileCredentialVault(settings)
    with pytest.raises(ValueError):
        store_api_key("not-a-key", settings=settings, vault=vault)
    assert vault.list_accounts() == []


def test_store_api_key_supports_multiple_accounts(settings) -> None:
    vault = FileCredentialVault(settings)
    store_api_key("sk-accountone000000", account_ref="acct-1", settings=settings, vault=vault)
    store_api_key("sk-accounttwo000000", account_ref="acct-2", settings=settings, vault=vault)
    assert vault.get("openai_api", "acct-1") == {"api_key": "sk-accountone000000"}
    assert vault.get("openai_api", "acct-2") == {"api_key": "sk-accounttwo000000"}
