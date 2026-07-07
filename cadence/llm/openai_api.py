"""API-key LLM provider — the fully-supported, recommended default.

Pay-per-token access to the OpenAI Responses API with a plain ``sk-...`` key. No ToS
gray zone, no message-count limits, no refresh dance — just ``Authorization: Bearer``.
Prefer this over :mod:`cadence.llm.chatgpt_oauth` for any production use.
"""

from __future__ import annotations

import httpx

from cadence.llm.provider import (
    LLMAuthError,
    LLMError,
    LLMProvider,
    LLMRequest,
    LLMResponse,
)
from cadence.obs.egress import RawEgressLog

#: Documented default; overridable via ``CADENCE_OPENAI_API_BASE`` (see Settings).
DEFAULT_OPENAI_API_BASE = "https://api.openai.com/v1"


class OpenAIAPIProvider(LLMProvider):
    """Responses-API provider authenticated with an OpenAI API key.

    ``POST {api_base}/responses`` with ``Authorization: Bearer sk-...``. The API key lives
    only in the request header — never logged, never in the egress audit (which stores a
    content hash, not headers).
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        api_base: str = DEFAULT_OPENAI_API_BASE,
        client: httpx.Client | None = None,
        timeout: float = 30.0,
        egress_log: RawEgressLog | None = None,
    ) -> None:
        self._api_base = api_base.rstrip("/")
        self._url = f"{self._api_base}/responses"
        super().__init__(destination=self._url, egress_log=egress_log)
        self._api_key = api_key
        self._model = model
        self._timeout = timeout
        self._client = client
        self._owns_client = client is None

    @property
    def model(self) -> str:
        return self._model

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self._timeout)
        return self._client

    def _send(self, request: LLMRequest) -> LLMResponse:
        try:
            resp = self._http().post(
                self._url,
                json=request.to_body(),
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
            )
        except httpx.HTTPError as exc:  # network/transport failure
            raise LLMError(f"openai_api request failed: {exc}") from exc

        if resp.status_code == 401:
            raise LLMAuthError("openai_api rejected the API key (401)")
        if resp.status_code >= 400:
            raise LLMError(f"openai_api responded {resp.status_code}: {_safe_error(resp)}")
        return LLMResponse.from_payload(resp.json(), model=self._model)

    def close(self) -> None:
        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None


def _safe_error(resp: httpx.Response) -> str:
    """A short, non-verbatim error hint from a failed response body."""
    try:
        data = resp.json()
    except ValueError:
        return "<non-json body>"
    err = data.get("error") if isinstance(data, dict) else None
    if isinstance(err, dict):
        return str(err.get("code") or err.get("type") or err.get("message") or err)
    return str(err or data)


__all__ = ["DEFAULT_OPENAI_API_BASE", "OpenAIAPIProvider"]
