"""LLM provider core: the abstraction, request/response shapes, credential schema, and
the mandatory raw-content egress guard.

Every provider speaks the OpenAI **Responses API** shape (``POST .../responses`` with a
``model`` + ``input``/``instructions`` body, an ``output`` array reply). Two live
transports exist — :class:`~cadence.llm.openai_api.OpenAIAPIProvider` (API key, the
supported default) and :class:`~cadence.llm.chatgpt_oauth.ChatGPTOAuthProvider` (opt-in,
gray-zone) — plus a network-free :class:`~cadence.llm.mock.MockLLMProvider` for tests.

Raw-content boundary
--------------------
Per Decision E the LLM is one of exactly two sanctioned raw-content egress channels. The
base :meth:`LLMProvider.complete` records **every** call through :class:`EgressGuard`
into :class:`~cadence.obs.egress.RawEgressLog` (``EgressChannel.LLM_TEXT``) **before** the
request leaves — a hash + byte length + provenance, never the verbatim payload. Because it
lives in the base class, no concrete provider (mock included) can skip the audit.
"""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from cadence.obs.egress import EgressChannel, EgressRecord, RawEgressLog, get_egress_log

# --------------------------------------------------------------------------- #
# Shared credential schema (vault contract shared with the onboarding module)
# --------------------------------------------------------------------------- #


@dataclass
class TokenSet:
    """OAuth token bundle — the vault credential schema shared by the provider (reader)
    and the onboarding login flow (writer).

    Stored NAS-only via :class:`~cadence.adapters.vault.FileCredentialVault` under
    ``provider="chatgpt_oauth"``, ``account_ref=<account_id>``. ``expires_at`` is an
    aware UTC datetime marking when ``access_token`` stops being valid (used for
    proactive refresh); ``None`` means "unknown / never proactively refresh".
    """

    access_token: str
    refresh_token: str | None = None
    id_token: str | None = None
    account_id: str | None = None
    expires_at: datetime | None = None

    def to_vault_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-safe dict for the vault (``expires_at`` → ISO8601)."""
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "id_token": self.id_token,
            "account_id": self.account_id,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
        }

    @classmethod
    def from_vault_dict(cls, data: dict[str, Any]) -> TokenSet:
        """Inverse of :meth:`to_vault_dict` — tolerant of a missing/naive ``expires_at``."""
        raw_expires = data.get("expires_at")
        expires_at: datetime | None = None
        if raw_expires:
            parsed = datetime.fromisoformat(raw_expires)
            expires_at = parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        return cls(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token"),
            id_token=data.get("id_token"),
            account_id=data.get("account_id"),
            expires_at=expires_at,
        )


# --------------------------------------------------------------------------- #
# Responses-API request/response shapes
# --------------------------------------------------------------------------- #


@dataclass
class LLMRequest:
    """A Responses-API request.

    ``instructions`` (system-ish steer) + ``input`` (the user content) map straight onto
    the Responses API body. ``source_event_ids`` is provenance carried into the egress
    audit record — it is never sent to the provider.
    """

    model: str
    input: str
    instructions: str | None = None
    max_output_tokens: int | None = None
    temperature: float | None = None
    source_event_ids: tuple[str, ...] = ()

    def to_body(self) -> dict[str, Any]:
        """The JSON body posted to a ``/responses`` endpoint (provenance excluded)."""
        body: dict[str, Any] = {"model": self.model, "input": self.input}
        if self.instructions is not None:
            body["instructions"] = self.instructions
        if self.max_output_tokens is not None:
            body["max_output_tokens"] = self.max_output_tokens
        if self.temperature is not None:
            body["temperature"] = self.temperature
        return body

    def egress_content(self) -> str:
        """The raw text that will cross the boundary — hashed (never stored) for the audit."""
        return f"{self.instructions or ''}\n{self.input}"


@dataclass
class LLMResponse:
    """A parsed Responses-API reply.

    ``text`` is the concatenated ``output_text`` (the model's answer); ``raw`` keeps the
    full decoded payload for callers that need structured output items or usage.
    """

    text: str
    model: str
    raw: dict[str, Any] = field(default_factory=dict)
    usage: dict[str, Any] | None = None

    @classmethod
    def from_payload(cls, payload: dict[str, Any], *, model: str) -> LLMResponse:
        return cls(
            text=extract_output_text(payload),
            model=payload.get("model", model),
            raw=payload,
            usage=payload.get("usage"),
        )


def extract_output_text(payload: dict[str, Any]) -> str:
    """Pull the assistant text out of a Responses-API payload.

    Prefers the top-level ``output_text`` convenience field; otherwise walks the
    ``output`` array concatenating every ``output_text`` content part. Tolerant of shape
    drift — anything unrecognized yields ``""`` rather than raising, so a surprising
    payload degrades to "no text" (which the inference layer treats as "no deadlines")
    instead of crashing the pipeline.
    """
    convenience = payload.get("output_text")
    if isinstance(convenience, str):
        return convenience
    parts: list[str] = []
    for item in payload.get("output", []) or []:
        if not isinstance(item, dict):
            continue
        for content in item.get("content", []) or []:
            if isinstance(content, dict) and content.get("type") in ("output_text", "text"):
                text = content.get("text")
                if isinstance(text, str):
                    parts.append(text)
    return "".join(parts)


# --------------------------------------------------------------------------- #
# Egress guard
# --------------------------------------------------------------------------- #


class EgressGuard:
    """Records every raw-content LLM call into the sanctioned egress ledger.

    Called by :meth:`LLMProvider.complete` **before** the request is transmitted. Stores a
    SHA-256 of the outbound text + byte length + provenance ids — never the verbatim text
    (the raw boundary applies to the audit log too).
    """

    def __init__(self, destination: str, egress_log: RawEgressLog | None = None) -> None:
        self._destination = destination
        self._log = egress_log or get_egress_log()

    def record(self, request: LLMRequest) -> EgressRecord:
        content = request.egress_content()
        encoded = content.encode("utf-8")
        return self._log.record(
            EgressChannel.LLM_TEXT,
            content_hash=hashlib.sha256(encoded).hexdigest(),
            destination=self._destination,
            byte_len=len(encoded),
            source_event_ids=request.source_event_ids,
        )


# --------------------------------------------------------------------------- #
# Provider ABC
# --------------------------------------------------------------------------- #


class LLMError(RuntimeError):
    """Base class for provider-transport failures surfaced to callers."""


class LLMAuthError(LLMError):
    """Authentication/authorization failure (e.g. expired or rejected credential)."""


class MissingCodexEntitlementError(LLMAuthError):
    """The ChatGPT account lacks the Codex entitlement the inference endpoint requires."""


class LLMProvider(ABC):
    """Base LLM provider. Speaks the Responses API and enforces egress auditing.

    Subclasses implement :meth:`_send` (the transport). :meth:`complete` is a template
    method: it audits the call through :class:`EgressGuard` first, so **no** provider can
    egress raw content without a ledger entry.
    """

    def __init__(self, *, destination: str, egress_log: RawEgressLog | None = None) -> None:
        self._destination = destination
        self._egress = EgressGuard(destination, egress_log)

    def complete(self, request: LLMRequest) -> LLMResponse:
        """Audit the raw-content egress, then send the request and return the reply."""
        self._egress.record(request)
        return self._send(request)

    @abstractmethod
    def _send(self, request: LLMRequest) -> LLMResponse:
        """Transmit ``request`` to the provider and return the parsed reply."""


__all__ = [
    "EgressGuard",
    "LLMAuthError",
    "LLMError",
    "LLMProvider",
    "LLMRequest",
    "LLMResponse",
    "MissingCodexEntitlementError",
    "TokenSet",
    "extract_output_text",
]
