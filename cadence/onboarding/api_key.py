"""Onboarding for the API-key provider: validate + store an OpenAI ``sk-...`` key.

This is the fully-supported, recommended default (:mod:`cadence.llm.openai_api`) — no
gray zone, no browser dance, no refresh/revoke lifecycle. Validation here is a shape
check only (looks like a plausible key); it never makes a live call to confirm the key
actually works — :class:`~cadence.llm.openai_api.OpenAIAPIProvider` finds that out
lazily on the first real request.
"""

from __future__ import annotations

import re

from cadence.adapters.base import CredentialVault
from cadence.adapters.vault import FileCredentialVault
from cadence.config import Settings, get_settings

#: Vault provider key under which the onboarding flow stores the API key.
VAULT_PROVIDER = "openai_api"

#: The default account_ref for the (typically single) API-key account.
DEFAULT_ACCOUNT_REF = "default"

_API_KEY_RE = re.compile(r"^sk-[A-Za-z0-9_-]{10,}$")


def validate_api_key(api_key: str) -> None:
    """Raise ``ValueError`` if ``api_key`` doesn't look like a plausible OpenAI API key."""
    if not _API_KEY_RE.match(api_key.strip()):
        raise ValueError(
            "not a plausible OpenAI API key (expected an 'sk-' prefix followed by the key body)"
        )


def store_api_key(
    api_key: str,
    *,
    account_ref: str = DEFAULT_ACCOUNT_REF,
    settings: Settings | None = None,
    vault: CredentialVault | None = None,
) -> None:
    """Validate ``api_key`` and store it in the NAS-only vault under ``openai_api``."""
    api_key = api_key.strip()
    validate_api_key(api_key)
    settings = settings or get_settings()
    v = vault or FileCredentialVault(settings)
    v.store(VAULT_PROVIDER, account_ref, {"api_key": api_key})


__all__ = ["DEFAULT_ACCOUNT_REF", "VAULT_PROVIDER", "store_api_key", "validate_api_key"]
