"""LLM provider factory — the explicit, gated activation seam (Component B).

Nothing else in Cadence auto-instantiates an :class:`~cadence.llm.provider.LLMProvider`
from config — this module is the *only* place that does, and only when every one of
three independent conditions holds: ``settings.llm_enabled`` is ``True``,
``settings.llm_provider`` names a real transport (not the default ``"none"``), and the
vault already holds the matching credential. Onboarding (:mod:`cadence.onboarding.*`) is
the only writer of that credential — this module only reads. Any missing precondition
yields ``None``, never an exception, so a not-yet-onboarded or misconfigured deployment
degrades to "no LLM" instead of crashing a tick. See ``cadence/llm/README.md`` and
``.omc/plans/cadence-milestone-5-runtime.md`` (Component B).

:func:`build_llm_provider` builds the bare transport; :func:`build_deadline_llm_hook` and
:func:`build_receptiveness_hook` wrap it in the inference adapters
(:mod:`cadence.llm.inference`) and return the plain callable hook — or ``None`` — that the
runtime service wires into ``RuleDeadlineExtractor(llm_hook=...)`` /
``NudgeGovernor(receptiveness_hook=...)`` **only when non-``None``**. Even then the
governor keeps running in shadow mode until S0.2 calibration passes (a separate gate this
module does not touch).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from cadence.adapters.base import CredentialVault, Event
from cadence.brain.deadlines import DeadlineCandidate
from cadence.config import Settings
from cadence.llm.chatgpt_oauth import VAULT_PROVIDER as CHATGPT_OAUTH_VAULT_PROVIDER
from cadence.llm.chatgpt_oauth import ChatGPTOAuthProvider
from cadence.llm.inference import LLMDeadlineInference, LLMReceptiveness
from cadence.llm.openai_api import OpenAIAPIProvider
from cadence.llm.provider import LLMProvider
from cadence.obs.logging import get_logger, log_event
from cadence.onboarding.api_key import DEFAULT_ACCOUNT_REF as OPENAI_API_ACCOUNT_REF
from cadence.onboarding.api_key import VAULT_PROVIDER as OPENAI_API_VAULT_PROVIDER
from cadence.onboarding.chatgpt_oauth import make_refresh_callable

_LOG = get_logger("llm.factory")


def _off(settings: Settings, reason: str, **fields: Any) -> None:
    """Log why the factory is returning ``None`` and return it — the shared "off" exit."""
    log_event(
        _LOG, 20, "llm_provider_not_built",
        reason=reason, llm_provider=settings.llm_provider, **fields,
    )
    return None


def build_llm_provider(settings: Settings, vault: CredentialVault) -> LLMProvider | None:
    """Build the configured, onboarded LLM transport — or ``None`` if any gate fails.

    Gates, each independently sufficient to keep the LLM off:

    1. ``settings.llm_enabled`` — the go-live opt-in.
    2. ``settings.llm_provider`` names a real transport, not ``"none"``.
    3. The vault already holds the matching onboarded credential.
    """
    if not settings.llm_enabled:
        return _off(settings, "llm_disabled")
    if settings.llm_provider == "none":
        return _off(settings, "llm_provider_none")
    if settings.llm_provider == "openai_api":
        return _build_openai_api(settings, vault)
    if settings.llm_provider == "chatgpt_oauth":
        return _build_chatgpt_oauth(settings, vault)
    return _off(settings, "llm_provider_unknown")


def _build_openai_api(settings: Settings, vault: CredentialVault) -> LLMProvider | None:
    try:
        secret = vault.get(OPENAI_API_VAULT_PROVIDER, OPENAI_API_ACCOUNT_REF)
    except KeyError:
        return _off(settings, "no_vault_credential")
    api_key = secret.get("api_key")
    if not api_key:
        return _off(settings, "vault_credential_missing_api_key")
    return OpenAIAPIProvider(
        api_key=api_key, model=settings.llm_model, api_base=settings.openai_api_base
    )


def _build_chatgpt_oauth(settings: Settings, vault: CredentialVault) -> LLMProvider | None:
    accounts = [
        ref for provider, ref in vault.list_accounts() if provider == CHATGPT_OAUTH_VAULT_PROVIDER
    ]
    if not accounts:
        return _off(settings, "no_vault_credential")
    # Onboarding stores one ChatGPT account under its own account_id; if more than one was
    # ever onboarded, the first (oldest) wins rather than guessing — a documented plug-in
    # point, not a silent ambiguity.
    account_ref = accounts[0]
    return ChatGPTOAuthProvider(
        vault=vault,
        account_ref=account_ref,
        model=settings.llm_model,
        refresh_callable=make_refresh_callable(settings),
        chatgpt_base=settings.chatgpt_base,
    )


def build_deadline_llm_hook(
    settings: Settings, vault: CredentialVault
) -> Callable[[Event], list[DeadlineCandidate]] | None:
    """Build the ``RuleDeadlineExtractor`` ``llm_hook`` — or ``None`` when the LLM is off."""
    provider = build_llm_provider(settings, vault)
    if provider is None:
        return None
    inference = LLMDeadlineInference(provider, model=settings.llm_model, enabled=True)
    return inference.as_hook()


def build_receptiveness_hook(
    settings: Settings, vault: CredentialVault
) -> Callable[..., float] | None:
    """Build the ``NudgeGovernor`` ``receptiveness_hook`` — or ``None`` when the LLM is off."""
    provider = build_llm_provider(settings, vault)
    if provider is None:
        return None
    inference = LLMReceptiveness(provider, model=settings.llm_model, enabled=True)
    return inference.as_hook()


__all__ = ["build_deadline_llm_hook", "build_llm_provider", "build_receptiveness_hook"]
