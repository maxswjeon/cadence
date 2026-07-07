"""Live-transport tests against a FAKE local Responses server (stdlib http.server).

No live OpenAI, no real credentials. Exercises the request shape (URL + headers + body)
of both providers, the ChatGPT-OAuth refresh-on-401 + proactive-refresh + token
re-persistence, entitlement-error handling, and the tokens-never-in-logs invariant.
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from cadence.adapters.vault import FileCredentialVault
from cadence.llm import (
    ChatGPTOAuthProvider,
    LLMAuthError,
    LLMRequest,
    MissingCodexEntitlementError,
    OpenAIAPIProvider,
    TokenSet,
)
from cadence.llm.chatgpt_oauth import VAULT_PROVIDER
from cadence.obs.egress import EgressChannel, get_egress_log

_OK_BODY = {"model": "srv", "output": [{"content": [{"type": "output_text", "text": "hi"}]}]}


class _FakeResponsesServer:
    """A stdlib HTTP server that records requests and returns programmed responses.

    ``responder(path, headers, body) -> (status, dict)`` is called per request; the server
    appends each ``(path, headers, body)`` to :attr:`requests` for assertions.
    """

    def __init__(self, responder) -> None:
        self.requests: list[tuple[str, dict[str, str], dict]] = []
        server_self = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # silence stderr noise
                pass

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length) if length else b"{}"
                body = json.loads(raw or b"{}")
                headers = dict(self.headers.items())
                server_self.requests.append((self.path, headers, body))
                status, payload = responder(self.path, headers, body)
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self._httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    def __enter__(self) -> _FakeResponsesServer:
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"


# -- API-key provider ------------------------------------------------------- #


def test_openai_api_request_shape_and_auth_header() -> None:
    with _FakeResponsesServer(lambda p, h, b: (200, _OK_BODY)) as srv:
        provider = OpenAIAPIProvider(
            api_key="sk-secret-123", model="gpt-5.5", api_base=f"{srv.base}/v1"
        )
        resp = provider.complete(
            LLMRequest(model="gpt-5.5", input="hello", instructions="be terse")
        )
        provider.close()

    assert resp.text == "hi"
    path, headers, body = srv.requests[0]
    assert path == "/v1/responses"
    assert headers["Authorization"] == "Bearer sk-secret-123"
    assert body == {"model": "gpt-5.5", "input": "hello", "instructions": "be terse"}
    assert get_egress_log().count(EgressChannel.LLM_TEXT) == 1


def test_openai_api_401_raises_auth_error() -> None:
    with _FakeResponsesServer(lambda p, h, b: (401, {"error": {"code": "invalid_api_key"}})) as srv:
        provider = OpenAIAPIProvider(api_key="sk-bad", model="m", api_base=f"{srv.base}/v1")
        with pytest.raises(LLMAuthError):
            provider.complete(LLMRequest(model="m", input="x"))
        provider.close()


# -- ChatGPT-OAuth provider ------------------------------------------------- #


def _vault(settings) -> FileCredentialVault:
    return FileCredentialVault(settings)


def _seed_tokens(vault, tokens: TokenSet, account_ref: str) -> None:
    vault.store(VAULT_PROVIDER, account_ref, tokens.to_vault_dict())


def test_chatgpt_oauth_request_headers_and_shape(settings) -> None:
    vault = _vault(settings)
    _seed_tokens(vault, TokenSet(access_token="tok-A", account_id="acct-9"), "acct-9")

    with _FakeResponsesServer(lambda p, h, b: (200, _OK_BODY)) as srv:
        provider = ChatGPTOAuthProvider(
            vault=vault,
            account_ref="acct-9",
            model="gpt-5.5",
            refresh_callable=lambda ts: ts,
            chatgpt_base=f"{srv.base}/backend-api/codex",
            originator="codex_cli_rs",
        )
        provider.complete(LLMRequest(model="gpt-5.5", input="hi"))
        provider.close()

    path, headers, _ = srv.requests[0]
    assert path == "/backend-api/codex/responses"
    assert headers["Authorization"] == "Bearer tok-A"
    assert headers["ChatGPT-Account-ID"] == "acct-9"
    assert headers["OpenAI-Beta"] == "responses=experimental"
    assert headers["originator"] == "codex_cli_rs"


def test_chatgpt_oauth_refresh_on_401_retries_and_repersists(settings) -> None:
    vault = _vault(settings)
    _seed_tokens(
        vault, TokenSet(access_token="expired", refresh_token="r1", account_id="acct-9"), "acct-9"
    )

    def responder(path, headers, body):
        # The expired token is rejected; the refreshed one is accepted.
        if headers.get("Authorization") == "Bearer expired":
            return 401, {"error": {"code": "token_expired"}}
        return 200, _OK_BODY

    refreshed = TokenSet(access_token="fresh", refresh_token="r2", account_id="acct-9")

    with _FakeResponsesServer(responder) as srv:
        provider = ChatGPTOAuthProvider(
            vault=vault,
            account_ref="acct-9",
            model="m",
            refresh_callable=lambda ts: refreshed,
            chatgpt_base=f"{srv.base}/backend-api/codex",
        )
        resp = provider.complete(LLMRequest(model="m", input="hi"))
        provider.close()

    assert resp.text == "hi"
    # Two attempts: the 401 then the retry with the fresh token.
    sent_auth = [h.get("Authorization") for _, h, _ in srv.requests]
    assert sent_auth == ["Bearer expired", "Bearer fresh"]
    # Rotated tokens were re-persisted to the vault.
    assert TokenSet.from_vault_dict(vault.get(VAULT_PROVIDER, "acct-9")).access_token == "fresh"


def test_chatgpt_oauth_proactive_refresh_before_expiry(settings) -> None:
    vault = _vault(settings)
    past = datetime.now(tz=UTC) - timedelta(seconds=10)  # already expired
    _seed_tokens(
        vault,
        TokenSet(access_token="stale", refresh_token="r1", account_id="acct-9", expires_at=past),
        "acct-9",
    )
    refreshed = TokenSet(access_token="proactive", refresh_token="r1", account_id="acct-9")

    with _FakeResponsesServer(lambda p, h, b: (200, _OK_BODY)) as srv:
        provider = ChatGPTOAuthProvider(
            vault=vault,
            account_ref="acct-9",
            model="m",
            refresh_callable=lambda ts: refreshed,
            chatgpt_base=f"{srv.base}/backend-api/codex",
        )
        provider.complete(LLMRequest(model="m", input="hi"))
        provider.close()

    # The stale token never went out — proactive refresh replaced it before the first call.
    assert srv.requests[0][1].get("Authorization") == "Bearer proactive"


def test_chatgpt_oauth_missing_entitlement_raises_clear_error(settings) -> None:
    vault = _vault(settings)
    _seed_tokens(vault, TokenSet(access_token="tok", account_id="acct-9"), "acct-9")

    def responder(path, headers, body):
        return 403, {"error": {"code": "missing_codex_entitlement"}}

    with _FakeResponsesServer(responder) as srv:
        provider = ChatGPTOAuthProvider(
            vault=vault,
            account_ref="acct-9",
            model="m",
            refresh_callable=lambda ts: ts,
            chatgpt_base=f"{srv.base}/backend-api/codex",
        )
        with pytest.raises(MissingCodexEntitlementError):
            provider.complete(LLMRequest(model="m", input="hi"))
        provider.close()


def test_tokens_never_appear_in_logs_or_egress(settings, caplog) -> None:
    vault = _vault(settings)
    secret_token = "super-secret-access-token-xyz"
    _seed_tokens(vault, TokenSet(access_token=secret_token, account_id="acct-9"), "acct-9")

    with caplog.at_level("DEBUG", logger="cadence"), _FakeResponsesServer(
        lambda p, h, b: (200, _OK_BODY)
    ) as srv:
        provider = ChatGPTOAuthProvider(
            vault=vault,
            account_ref="acct-9",
            model="m",
            refresh_callable=lambda ts: ts,
            chatgpt_base=f"{srv.base}/backend-api/codex",
        )
        provider.complete(LLMRequest(model="m", input="hi"))
        provider.close()

    # Not in any log record.
    assert secret_token not in caplog.text
    # Not in the egress audit (which stores a hash + metadata only).
    for rec in get_egress_log().records():
        assert secret_token not in rec.content_hash
        assert secret_token not in rec.destination
