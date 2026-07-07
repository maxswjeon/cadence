"""Live LLM inference core for Cadence.

Provider abstraction (Responses-API shaped) + two live transports and a mock, plus the
inference adapters that wire into the brain's ``llm_hook`` / ``receptiveness_hook`` seams.

Posture (see ``README.md``): the **API-key provider is the supported default**; the
**ChatGPT-OAuth provider is opt-in / gray-zone / may-break**; and inference is **off by
default**, shadow-gated until S0.2 calibration passes.
"""

from __future__ import annotations

from cadence.llm.chatgpt_oauth import ChatGPTOAuthProvider
from cadence.llm.inference import LLMDeadlineInference, LLMReceptiveness
from cadence.llm.mock import MockLLMProvider
from cadence.llm.openai_api import OpenAIAPIProvider
from cadence.llm.provider import (
    EgressGuard,
    LLMAuthError,
    LLMError,
    LLMProvider,
    LLMRequest,
    LLMResponse,
    MissingCodexEntitlementError,
    TokenSet,
)

__all__ = [
    "ChatGPTOAuthProvider",
    "EgressGuard",
    "LLMAuthError",
    "LLMDeadlineInference",
    "LLMError",
    "LLMProvider",
    "LLMReceptiveness",
    "LLMRequest",
    "LLMResponse",
    "MissingCodexEntitlementError",
    "MockLLMProvider",
    "OpenAIAPIProvider",
    "TokenSet",
]
