"""Deterministic, network-free LLM provider for tests.

Canned Responses-API replies keyed by a substring found in the request ``input`` (or an
exact match), with a configurable default. Still routes through the base
:meth:`~cadence.llm.provider.LLMProvider.complete`, so egress auditing is exercised
exactly as it is for the live transports — the mock only replaces the network hop.
"""

from __future__ import annotations

from cadence.llm.provider import LLMProvider, LLMRequest, LLMResponse
from cadence.obs.egress import RawEgressLog


def _text_payload(text: str, *, model: str) -> dict:
    """A minimal but shape-accurate Responses-API payload wrapping ``text``."""
    return {
        "model": model,
        "output": [{"content": [{"type": "output_text", "text": text}]}],
    }


class MockLLMProvider(LLMProvider):
    """Returns canned replies; never touches the network.

    ``replies`` maps a substring → the assistant text to return when that substring
    appears in ``request.input``. The first matching key (insertion order) wins;
    ``default`` is used when nothing matches. Records every request in
    :attr:`calls` for assertions.
    """

    def __init__(
        self,
        replies: dict[str, str] | None = None,
        *,
        default: str = "{}",
        model: str = "mock-model",
        egress_log: RawEgressLog | None = None,
    ) -> None:
        super().__init__(destination="mock", egress_log=egress_log)
        self._replies = dict(replies or {})
        self._default = default
        self._model = model
        self.calls: list[LLMRequest] = []

    def _reply_for(self, request: LLMRequest) -> str:
        for needle, reply in self._replies.items():
            if needle in request.input:
                return reply
        return self._default

    def _send(self, request: LLMRequest) -> LLMResponse:
        self.calls.append(request)
        payload = _text_payload(self._reply_for(request), model=self._model)
        return LLMResponse.from_payload(payload, model=self._model)


__all__ = ["MockLLMProvider"]
