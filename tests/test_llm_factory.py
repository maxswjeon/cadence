"""Factory-gating tests (Component B): the LLM stays off unless every gate is satisfied.

No live network anywhere here — the "credentialed" cases only prove the factory builds
the right provider *type* from a fake vault credential; the hook-flow tests inject a
:class:`MockLLMProvider` via monkeypatch so nothing ever reaches a socket.
"""

from __future__ import annotations

from datetime import UTC, datetime

from cadence.adapters.base import AcquisitionTier, Event
from cadence.adapters.vault import FileCredentialVault
from cadence.llm import ChatGPTOAuthProvider, MockLLMProvider, OpenAIAPIProvider, TokenSet
from cadence.llm.chatgpt_oauth import VAULT_PROVIDER as CHATGPT_OAUTH_VAULT_PROVIDER
from cadence.llm.factory import (
    build_deadline_llm_hook,
    build_llm_provider,
    build_receptiveness_hook,
)
from cadence.onboarding.api_key import DEFAULT_ACCOUNT_REF as OPENAI_API_ACCOUNT_REF
from cadence.onboarding.api_key import VAULT_PROVIDER as OPENAI_API_VAULT_PROVIDER


def _event(event_id: str = "e1", **kw) -> Event:
    return Event(
        event_id=event_id,
        source="test",
        account_ref="acct",
        kind=kw.pop("kind", "email.message"),
        acquisition_tier=AcquisitionTier.MANUAL,
        occurred_at=kw.pop("occurred_at", datetime(2026, 7, 5, tzinfo=UTC)),
        **kw,
    )


# -- the default is off ------------------------------------------------------ #


def test_default_settings_yield_none(settings) -> None:
    assert settings.llm_enabled is False
    assert settings.llm_provider == "none"
    vault = FileCredentialVault(settings)
    assert build_llm_provider(settings, vault) is None


# -- each gate independently keeps the LLM off ------------------------------- #


def test_llm_disabled_returns_none_even_with_provider_and_credential(settings) -> None:
    vault = FileCredentialVault(settings)
    vault.store(OPENAI_API_VAULT_PROVIDER, OPENAI_API_ACCOUNT_REF, {"api_key": "sk-fake000000"})
    disabled = settings.model_copy(update={"llm_enabled": False, "llm_provider": "openai_api"})
    assert build_llm_provider(disabled, vault) is None


def test_llm_provider_none_returns_none_even_when_enabled(settings) -> None:
    vault = FileCredentialVault(settings)
    off = settings.model_copy(update={"llm_enabled": True, "llm_provider": "none"})
    assert build_llm_provider(off, vault) is None


def test_openai_api_without_vault_credential_returns_none(settings) -> None:
    vault = FileCredentialVault(settings)  # no credential stored
    enabled = settings.model_copy(update={"llm_enabled": True, "llm_provider": "openai_api"})
    assert build_llm_provider(enabled, vault) is None


def test_chatgpt_oauth_without_vault_credential_returns_none(settings) -> None:
    vault = FileCredentialVault(settings)  # no credential stored
    enabled = settings.model_copy(update={"llm_enabled": True, "llm_provider": "chatgpt_oauth"})
    assert build_llm_provider(enabled, vault) is None


def test_unknown_provider_returns_none(settings) -> None:
    vault = FileCredentialVault(settings)
    enabled = settings.model_copy(update={"llm_enabled": True, "llm_provider": "bogus"})
    assert build_llm_provider(enabled, vault) is None


# -- enabled + onboarded builds the right provider TYPE (no live network) --- #


def test_openai_api_builds_provider_when_enabled_and_credentialed(settings) -> None:
    vault = FileCredentialVault(settings)
    vault.store(OPENAI_API_VAULT_PROVIDER, OPENAI_API_ACCOUNT_REF, {"api_key": "sk-fake000000"})
    enabled = settings.model_copy(
        update={"llm_enabled": True, "llm_provider": "openai_api", "llm_model": "gpt-5.5"}
    )
    provider = build_llm_provider(enabled, vault)
    assert isinstance(provider, OpenAIAPIProvider)
    assert provider.model == "gpt-5.5"


def test_chatgpt_oauth_builds_provider_when_enabled_and_credentialed(settings) -> None:
    vault = FileCredentialVault(settings)
    tokens = TokenSet(access_token="tok", refresh_token="ref", account_id="acct-9")
    vault.store(CHATGPT_OAUTH_VAULT_PROVIDER, "acct-9", tokens.to_vault_dict())
    enabled = settings.model_copy(
        update={"llm_enabled": True, "llm_provider": "chatgpt_oauth", "llm_model": "gpt-5.5"}
    )
    provider = build_llm_provider(enabled, vault)
    assert isinstance(provider, ChatGPTOAuthProvider)
    assert provider.model == "gpt-5.5"


# -- hooks: None when off, and flow an Event -> DeadlineCandidate when on --- #


def test_build_deadline_llm_hook_is_none_by_default(settings) -> None:
    vault = FileCredentialVault(settings)
    assert build_deadline_llm_hook(settings, vault) is None


def test_build_receptiveness_hook_is_none_by_default(settings) -> None:
    vault = FileCredentialVault(settings)
    assert build_receptiveness_hook(settings, vault) is None


_GOOD_DEADLINE_JSON = (
    '{"deadlines": [{"due_at": "2026-07-15T17:00:00Z", "confidence": 0.7, '
    '"summary": "invoice due"}]}'
)


def test_build_deadline_llm_hook_flows_event_to_candidate(settings, monkeypatch) -> None:
    mock_provider = MockLLMProvider({"invoice": _GOOD_DEADLINE_JSON})
    monkeypatch.setattr(
        "cadence.llm.factory.build_llm_provider", lambda *_a, **_kw: mock_provider
    )
    enabled = settings.model_copy(
        update={"llm_enabled": True, "llm_provider": "openai_api", "llm_model": "gpt-5.5"}
    )
    hook = build_deadline_llm_hook(enabled, FileCredentialVault(settings))
    assert hook is not None

    candidates = hook(_event(summary="Please pay the invoice soon"))
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.origin == "inferred"
    assert candidate.confidence_type == "llm"
    assert candidate.confidence_value == 0.7
    assert candidate.due_at == datetime(2026, 7, 15, 17, 0, tzinfo=UTC)
    assert mock_provider.calls  # the provider was actually invoked, not skipped


def test_build_receptiveness_hook_flows_through_mock_provider(settings, monkeypatch) -> None:
    mock_provider = MockLLMProvider(default='{"receptiveness": 0.5}')
    monkeypatch.setattr(
        "cadence.llm.factory.build_llm_provider", lambda *_a, **_kw: mock_provider
    )
    enabled = settings.model_copy(update={"llm_enabled": True, "llm_provider": "openai_api"})
    hook = build_receptiveness_hook(enabled, FileCredentialVault(settings))
    assert hook is not None

    class _Cand:
        confidence = 0.8
        message_summary = "nudge"

    assert hook(_Cand(), None) == 0.4
    assert mock_provider.calls
