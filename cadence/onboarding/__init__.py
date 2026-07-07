"""Onboarding login flows for Cadence's LLM providers (Milestone 4, Component B).

Two auth paths, matching :mod:`cadence.llm`'s providers:

* :mod:`cadence.onboarding.api_key` — validate + store an OpenAI ``sk-...`` API key
  (the fully-supported default; no gray zone, no browser dance).
* :mod:`cadence.onboarding.chatgpt_oauth` — browser-based OAuth 2.0 Authorization Code
  + PKCE login against a ChatGPT account (opt-in, **gray-zone** — see that module's
  docstring), plus a headless device-code fallback. This module owns ``refresh``/
  ``revoke`` for the resulting :class:`~cadence.llm.provider.TokenSet`;
  :class:`cadence.llm.chatgpt_oauth.ChatGPTOAuthProvider` calls back into them (see
  :func:`~cadence.onboarding.chatgpt_oauth.make_refresh_callable`).

Run ``python -m cadence.onboarding`` for the interactive CLI. Neither path ever prints
a token, refresh token, id_token, or API key.
"""

from __future__ import annotations

from cadence.onboarding.api_key import store_api_key, validate_api_key
from cadence.onboarding.chatgpt_oauth import (
    ChatGPTOAuthOnboarding,
    DeviceAuthSession,
    make_refresh_callable,
    poll_device_login,
    refresh,
    refresh_and_persist,
    revoke,
    start_device_login,
)

__all__ = [
    "ChatGPTOAuthOnboarding",
    "DeviceAuthSession",
    "make_refresh_callable",
    "poll_device_login",
    "refresh",
    "refresh_and_persist",
    "revoke",
    "start_device_login",
    "store_api_key",
    "validate_api_key",
]
