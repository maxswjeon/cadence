"""ChatGPT-OAuth LLM provider — opt-in, config-driven, GRAY ZONE.

.. warning::
   This transport reaches the ChatGPT backend the way the Codex CLI does: it presents the
   public Codex ``client_id`` + ``originator`` and an OAuth ``access_token`` minted for a
   ChatGPT account. It is **unsupported for non-Codex use, may break without notice, is
   ToS-gray, and is subject to message-count rate limits.** Use
   :mod:`cadence.llm.openai_api` (pay-per-token API key) for anything real. Every
   endpoint / client_id / originator is *configuration with a documented default*
   (:class:`cadence.config.Settings`), never a silent hardcode.

``POST {chatgpt_base}/responses`` with headers ``Authorization: Bearer <access_token>``,
``ChatGPT-Account-ID: <account_id>``, ``OpenAI-Beta: responses=experimental``, and
``originator: <config>``. Tokens are read from the NAS-only vault as a
:class:`~cadence.llm.provider.TokenSet`. On a proactive-expiry check or a ``401`` the
provider calls the injected ``refresh_callable`` (owned by the onboarding module),
re-persists the rotated tokens to the vault, and retries once.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx

from cadence.adapters.base import CredentialVault
from cadence.llm.provider import (
    LLMAuthError,
    LLMError,
    LLMProvider,
    LLMRequest,
    LLMResponse,
    MissingCodexEntitlementError,
    TokenSet,
)
from cadence.obs.egress import RawEgressLog

#: Documented defaults (Codex CLI values); all overridable via Settings/env.
DEFAULT_CHATGPT_BASE = "https://chatgpt.com/backend-api/codex"
DEFAULT_ORIGINATOR = "codex_cli_rs"

#: Vault provider key under which the onboarding flow stores the ChatGPT ``TokenSet``.
VAULT_PROVIDER = "chatgpt_oauth"

#: Refresh the access token this long before ``expires_at`` (proactive refresh skew).
_REFRESH_SKEW = timedelta(seconds=120)

RefreshCallable = Callable[[TokenSet], TokenSet]


class ChatGPTOAuthProvider(LLMProvider):
    """Gray-zone Responses-API provider backed by a ChatGPT-account OAuth token."""

    def __init__(
        self,
        *,
        vault: CredentialVault,
        account_ref: str,
        model: str,
        refresh_callable: RefreshCallable,
        chatgpt_base: str = DEFAULT_CHATGPT_BASE,
        originator: str = DEFAULT_ORIGINATOR,
        client: httpx.Client | None = None,
        timeout: float = 30.0,
        egress_log: RawEgressLog | None = None,
    ) -> None:
        self._chatgpt_base = chatgpt_base.rstrip("/")
        self._url = f"{self._chatgpt_base}/responses"
        super().__init__(destination=self._url, egress_log=egress_log)
        self._vault = vault
        self._account_ref = account_ref
        self._model = model
        self._refresh_callable = refresh_callable
        self._originator = originator
        self._timeout = timeout
        self._client = client
        self._owns_client = client is None

    @property
    def model(self) -> str:
        return self._model

    # -- token lifecycle ---------------------------------------------------- #

    def _load_tokens(self) -> TokenSet:
        return TokenSet.from_vault_dict(self._vault.get(VAULT_PROVIDER, self._account_ref))

    def _persist_tokens(self, tokens: TokenSet) -> None:
        self._vault.store(VAULT_PROVIDER, self._account_ref, tokens.to_vault_dict())

    def _refresh_and_persist(self, tokens: TokenSet) -> TokenSet:
        rotated = self._refresh_callable(tokens)
        self._persist_tokens(rotated)
        return rotated

    def _is_expired(self, tokens: TokenSet, now: datetime) -> bool:
        return tokens.expires_at is not None and now >= tokens.expires_at - _REFRESH_SKEW

    # -- transport ---------------------------------------------------------- #

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self._timeout)
        return self._client

    def _headers(self, tokens: TokenSet) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {tokens.access_token}",
            "ChatGPT-Account-ID": tokens.account_id or self._account_ref,
            "OpenAI-Beta": "responses=experimental",
            "originator": self._originator,
            "Content-Type": "application/json",
        }

    def _post(self, tokens: TokenSet, request: LLMRequest) -> httpx.Response:
        try:
            return self._http().post(
                self._url, json=request.to_body(), headers=self._headers(tokens)
            )
        except httpx.HTTPError as exc:
            raise LLMError(f"chatgpt_oauth request failed: {exc}") from exc

    def _send(self, request: LLMRequest) -> LLMResponse:
        tokens = self._load_tokens()

        # Proactive refresh: rotate before we even try if the token is at/near expiry.
        if self._is_expired(tokens, datetime.now(tz=UTC)):
            tokens = self._refresh_and_persist(tokens)

        resp = self._post(tokens, request)

        # Reactive refresh-on-401: token may have been revoked/expired server-side ahead of
        # our clock. Refresh once and retry; a second 401 is a real auth failure.
        if resp.status_code == 401 and tokens.refresh_token:
            tokens = self._refresh_and_persist(tokens)
            resp = self._post(tokens, request)

        return self._handle(resp)

    def _handle(self, resp: httpx.Response) -> LLMResponse:
        if resp.status_code == 401:
            raise LLMAuthError("chatgpt_oauth rejected the access token (401)")
        if resp.status_code >= 400:
            code = _error_code(resp)
            if code and "codex" in code and "entitlement" in code:
                raise MissingCodexEntitlementError(
                    "this ChatGPT account lacks the Codex entitlement required by the "
                    "codex/responses endpoint — use the API-key provider instead "
                    f"(server said: {code})"
                )
            if resp.status_code == 403:
                raise LLMAuthError(f"chatgpt_oauth forbidden (403): {code or 'no detail'}")
            raise LLMError(f"chatgpt_oauth responded {resp.status_code}: {code or 'no detail'}")
        return LLMResponse.from_payload(resp.json(), model=self._model)

    def close(self) -> None:
        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None


def _error_code(resp: httpx.Response) -> str | None:
    """Extract a short error code/type/message from a failed response body (non-verbatim)."""
    try:
        data = resp.json()
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    err = data.get("error", data)
    if isinstance(err, dict):
        value = err.get("code") or err.get("type") or err.get("message")
        return str(value) if value is not None else None
    return str(err) if err is not None else None


__all__ = [
    "DEFAULT_CHATGPT_BASE",
    "DEFAULT_ORIGINATOR",
    "VAULT_PROVIDER",
    "ChatGPTOAuthProvider",
    "RefreshCallable",
]
