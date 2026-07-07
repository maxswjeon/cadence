"""Tests for the ChatGPT-OAuth onboarding flow.

Drives the FULL PKCE dance against a **fake local OAuth server** (stdlib
``http.server``) standing in for ``auth.openai.com`` — no live network to OpenAI
anywhere in this file. The fake server also backs the device-code fallback and the
refresh/revoke lifecycle helpers that :mod:`cadence.llm.chatgpt_oauth` calls back into.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import logging
import secrets
import threading
import urllib.parse
from dataclasses import dataclass, field

import httpx
import pytest

from cadence.adapters.vault import FileCredentialVault
from cadence.llm.provider import TokenSet
from cadence.onboarding import chatgpt_oauth as oauth_mod
from cadence.onboarding.chatgpt_oauth import (
    ChatGPTOAuthOnboarding,
    _account_id_from_id_token,
    _code_challenge,
    _new_code_verifier,
    build_authorize_url,
    make_refresh_callable,
    poll_device_login,
    refresh,
    refresh_and_persist,
    revoke,
    start_device_login,
)

# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _sha256_b64url(s: str) -> str:
    return _b64url(hashlib.sha256(s.encode("ascii")).digest())


def _fake_id_token(account_id: str) -> str:
    header = _b64url(json.dumps({"alg": "none", "typ": "JWT"}).encode())
    payload = _b64url(
        json.dumps({"https://api.openai.com/auth": {"chatgpt_account_id": account_id}}).encode()
    )
    return f"{header}.{payload}."


class _ListHandler(logging.Handler):
    """Captures log records directly (bypasses the ``cadence`` logger's
    ``propagate = False``, which would otherwise defeat ``caplog``)."""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


# --------------------------------------------------------------------------- #
# fake OAuth issuer (stands in for auth.openai.com)
# --------------------------------------------------------------------------- #


@dataclass
class _FakeIssuerState:
    account_id: str = "acct-fake-1"
    authorize_calls: list[dict[str, str]] = field(default_factory=list)
    pending_codes: dict[str, dict[str, str | None]] = field(default_factory=dict)
    revoke_calls: list[dict[str, str]] = field(default_factory=list)
    refresh_tokens_seen: list[str] = field(default_factory=list)
    device_poll_count: int = 0
    device_pending_rounds: int = 0


class _FakeIssuerServer(http.server.ThreadingHTTPServer):
    def __init__(self, address, handler_cls, *, state: _FakeIssuerState) -> None:
        super().__init__(address, handler_cls)
        self.state = state


class _FakeIssuerHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass

    def _body(self) -> dict[str, str]:
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b""
        if "application/json" in self.headers.get("Content-Type", ""):
            return json.loads(raw.decode()) if raw else {}
        return {k: v[0] for k, v in urllib.parse.parse_qs(raw.decode()).items()}

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        state: _FakeIssuerState = self.server.state  # type: ignore[attr-defined]
        if parsed.path == "/oauth/authorize":
            params = dict(urllib.parse.parse_qsl(parsed.query))
            state.authorize_calls.append(params)
            code = secrets.token_urlsafe(8)
            state.pending_codes[code] = {"challenge": params.get("code_challenge")}
            location = f"{params['redirect_uri']}?code={code}&state={params['state']}"
            self.send_response(302)
            self.send_header("Location", location)
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        state: _FakeIssuerState = self.server.state  # type: ignore[attr-defined]
        body = self._body()
        if parsed.path == "/oauth/token":
            self._handle_token(state, body)
        elif parsed.path == "/oauth/revoke":
            state.revoke_calls.append(body)
            self._send_json(200, {})
        elif parsed.path == "/api/accounts/deviceauth/usercode":
            self._send_json(
                200,
                {
                    "device_code": "devcode-1",
                    "user_code": "ABCD-1234",
                    "verification_uri": "https://auth.openai.com/codex/device",
                    "interval": 0,
                    "expires_in": 900,
                },
            )
        elif parsed.path == "/api/accounts/deviceauth/token":
            state.device_poll_count += 1
            if state.device_poll_count <= state.device_pending_rounds:
                self._send_json(200, {"error": "authorization_pending"})
                return
            code = secrets.token_urlsafe(8)
            state.pending_codes[code] = {"challenge": None}
            self._send_json(200, {"code": code})
        else:
            self.send_response(404)
            self.end_headers()

    def _handle_token(self, state: _FakeIssuerState, body: dict[str, str]) -> None:
        grant_type = body.get("grant_type")
        if grant_type == "authorization_code":
            entry = state.pending_codes.pop(body.get("code", ""), None)
            if entry is None:
                self._send_json(400, {"error": "invalid_grant"})
                return
            challenge = entry["challenge"]
            if challenge is not None:
                verifier = body.get("code_verifier", "")
                if _sha256_b64url(verifier) != challenge:
                    self._send_json(400, {"error": "invalid_grant"})
                    return
            self._send_json(
                200,
                {
                    "access_token": f"access-{body['code']}",
                    "refresh_token": f"refresh-{body['code']}",
                    "id_token": _fake_id_token(state.account_id),
                    "expires_in": 3600,
                },
            )
        elif grant_type == "refresh_token":
            rt = body.get("refresh_token", "")
            state.refresh_tokens_seen.append(rt)
            self._send_json(
                200,
                {
                    "access_token": f"rotated-access-{rt}",
                    "refresh_token": f"rotated-refresh-{rt}",
                    "id_token": _fake_id_token(state.account_id),
                    "expires_in": 3600,
                },
            )
        else:
            self._send_json(400, {"error": "unsupported_grant_type"})


@pytest.fixture
def fake_issuer():
    state = _FakeIssuerState()
    server = _FakeIssuerServer(("127.0.0.1", 0), _FakeIssuerHandler, state=state)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        yield f"http://127.0.0.1:{port}", state
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


# --------------------------------------------------------------------------- #
# PKCE + authorize URL
# --------------------------------------------------------------------------- #


def test_code_verifier_and_challenge_are_valid_pkce() -> None:
    verifier = _new_code_verifier()
    assert len(verifier) >= 64
    assert all(c.isalnum() or c in "-_" for c in verifier)  # unreserved base64url alphabet
    challenge = _code_challenge(verifier)
    assert challenge == _sha256_b64url(verifier)
    assert "=" not in challenge


def test_authorize_url_has_required_pkce_and_codex_params(settings) -> None:
    challenge = _code_challenge(_new_code_verifier())
    url = build_authorize_url(
        settings,
        redirect_uri="http://localhost:1455/auth/callback",
        code_challenge=challenge,
        state="st4te",
    )
    qs = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
    assert qs["response_type"] == "code"
    assert qs["client_id"] == settings.chatgpt_oauth_client_id
    assert qs["redirect_uri"] == "http://localhost:1455/auth/callback"
    assert qs["scope"] == settings.chatgpt_oauth_scopes
    assert qs["code_challenge"] == challenge
    assert qs["code_challenge_method"] == "S256"
    assert qs["id_token_add_organizations"] == "true"
    assert qs["codex_cli_simplified_flow"] == "true"
    assert qs["state"] == "st4te"
    assert qs["originator"] == settings.chatgpt_oauth_originator


# --------------------------------------------------------------------------- #
# id_token claim parsing
# --------------------------------------------------------------------------- #


def test_account_id_parsed_from_id_token_auth_claim() -> None:
    assert _account_id_from_id_token(_fake_id_token("acct-parsed")) == "acct-parsed"


def test_account_id_parsing_rejects_malformed_id_token() -> None:
    with pytest.raises(ValueError, match="JWT"):
        _account_id_from_id_token("not-a-jwt")
    empty_claim = f"{_b64url(b'{}')}.{_b64url(json.dumps({}).encode())}."
    with pytest.raises(ValueError, match="chatgpt_account_id"):
        _account_id_from_id_token(empty_claim)


# --------------------------------------------------------------------------- #
# full login flow (browser + loopback + PKCE + vault)
# --------------------------------------------------------------------------- #


def test_full_login_flow_exercises_pkce_and_stores_token(
    settings, fake_issuer, monkeypatch
) -> None:
    issuer_base, state = fake_issuer
    oauth_settings = settings.model_copy(
        update={
            "chatgpt_oauth_issuer": issuer_base,
            "chatgpt_oauth_redirect_port": 18455,
            "chatgpt_oauth_redirect_port_fallback": 18457,
        }
    )
    vault = FileCredentialVault(oauth_settings)

    def fake_open(url: str) -> None:
        # Stand-in for the user's browser: actually follow the authorize -> loopback
        # redirect chain, exercising the fake issuer AND our real callback listener.
        httpx.get(url, follow_redirects=True, timeout=10.0)

    monkeypatch.setattr(oauth_mod.webbrowser, "open", fake_open)

    token_set = ChatGPTOAuthOnboarding(oauth_settings, vault=vault).login(timeout=10.0)

    assert token_set.account_id == state.account_id
    assert token_set.access_token and token_set.refresh_token and token_set.id_token

    assert len(state.authorize_calls) == 1
    call = state.authorize_calls[0]
    assert call["code_challenge_method"] == "S256"
    assert call["client_id"] == oauth_settings.chatgpt_oauth_client_id
    assert call["originator"] == oauth_settings.chatgpt_oauth_originator
    assert call["scope"] == oauth_settings.chatgpt_oauth_scopes

    stored = vault.get("chatgpt_oauth", state.account_id)
    assert stored["access_token"] == token_set.access_token
    assert stored["refresh_token"] == token_set.refresh_token


def test_state_mismatch_is_rejected(settings, monkeypatch) -> None:
    oauth_settings = settings.model_copy(
        update={"chatgpt_oauth_redirect_port": 18460, "chatgpt_oauth_redirect_port_fallback": 18462}
    )

    def fake_open_with_wrong_state(url: str) -> None:
        qs = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
        params = {"code": "irrelevant", "state": "not-the-real-state"}
        httpx.get(qs["redirect_uri"], params=params, timeout=10.0)

    monkeypatch.setattr(oauth_mod.webbrowser, "open", fake_open_with_wrong_state)

    with pytest.raises(ValueError, match="state"):
        ChatGPTOAuthOnboarding(oauth_settings).login(timeout=10.0)


def test_login_never_logs_tokens(settings, fake_issuer, monkeypatch) -> None:
    issuer_base, state = fake_issuer
    oauth_settings = settings.model_copy(
        update={
            "chatgpt_oauth_issuer": issuer_base,
            "chatgpt_oauth_redirect_port": 18465,
            "chatgpt_oauth_redirect_port_fallback": 18467,
        }
    )
    vault = FileCredentialVault(oauth_settings)

    def fake_open(url: str) -> None:
        httpx.get(url, follow_redirects=True, timeout=10.0)

    monkeypatch.setattr(oauth_mod.webbrowser, "open", fake_open)

    handler = _ListHandler()
    logging.getLogger("cadence.onboarding.chatgpt_oauth").addHandler(handler)
    try:
        token_set = ChatGPTOAuthOnboarding(oauth_settings, vault=vault).login(timeout=10.0)
    finally:
        logging.getLogger("cadence.onboarding.chatgpt_oauth").removeHandler(handler)

    secrets_to_check = [token_set.access_token, token_set.refresh_token, token_set.id_token]
    for record in handler.records:
        haystack = record.getMessage() + json.dumps(
            getattr(record, "extra_fields", {}), default=str
        )
        for secret in secrets_to_check:
            assert secret not in haystack


# --------------------------------------------------------------------------- #
# refresh / revoke — the seam cadence.llm.chatgpt_oauth.ChatGPTOAuthProvider calls
# --------------------------------------------------------------------------- #


def test_bare_refresh_rotates_without_touching_vault(settings, fake_issuer) -> None:
    issuer_base, state = fake_issuer
    oauth_settings = settings.model_copy(update={"chatgpt_oauth_issuer": issuer_base})
    vault = FileCredentialVault(oauth_settings)
    original = TokenSet(
        access_token="old-access",
        refresh_token="old-refresh",
        id_token="old-id",
        account_id="acct-x",
    )

    rotated = refresh(original, settings=oauth_settings)

    assert rotated.access_token != original.access_token
    assert rotated.refresh_token != original.refresh_token
    assert state.refresh_tokens_seen == ["old-refresh"]
    with pytest.raises(KeyError):
        vault.get("chatgpt_oauth", "acct-x")


def test_refresh_and_persist_round_trips_through_vault(settings, fake_issuer) -> None:
    issuer_base, state = fake_issuer
    oauth_settings = settings.model_copy(update={"chatgpt_oauth_issuer": issuer_base})
    vault = FileCredentialVault(oauth_settings)
    original = TokenSet(
        access_token="old-access",
        refresh_token="old-refresh",
        id_token="old-id",
        account_id=state.account_id,
    )
    vault.store("chatgpt_oauth", state.account_id, original.to_vault_dict())

    rotated = refresh_and_persist(original, settings=oauth_settings, vault=vault)

    assert rotated.access_token != original.access_token
    stored = vault.get("chatgpt_oauth", state.account_id)
    assert stored["access_token"] == rotated.access_token
    assert stored["refresh_token"] == rotated.refresh_token


def test_make_refresh_callable_matches_provider_contract(settings, fake_issuer) -> None:
    """``ChatGPTOAuthProvider(refresh_callable=...)`` calls this with exactly one
    positional ``TokenSet`` argument — prove the bound callable satisfies that shape."""
    issuer_base, state = fake_issuer
    oauth_settings = settings.model_copy(update={"chatgpt_oauth_issuer": issuer_base})
    refresh_callable = make_refresh_callable(oauth_settings)
    ts = TokenSet(access_token="a", refresh_token="r", id_token="i", account_id=state.account_id)

    rotated = refresh_callable(ts)

    assert rotated.access_token != ts.access_token
    assert rotated.account_id == ts.account_id


def test_revoke_removes_local_credential_and_calls_remote(settings, fake_issuer) -> None:
    issuer_base, state = fake_issuer
    oauth_settings = settings.model_copy(update={"chatgpt_oauth_issuer": issuer_base})
    vault = FileCredentialVault(oauth_settings)
    ts = TokenSet(access_token="a", refresh_token="r", id_token="i", account_id=state.account_id)
    vault.store("chatgpt_oauth", state.account_id, ts.to_vault_dict())

    revoke(ts, settings=oauth_settings, vault=vault)

    assert len(state.revoke_calls) == 1
    with pytest.raises(KeyError):
        vault.get("chatgpt_oauth", state.account_id)


def test_revoke_removes_local_credential_even_if_remote_call_fails(settings) -> None:
    # Nothing listens on 127.0.0.1:1 — a fast, local-only connection failure (no live
    # network to any real host), proving revoke still cleans up the vault.
    oauth_settings = settings.model_copy(update={"chatgpt_oauth_issuer": "http://127.0.0.1:1"})
    vault = FileCredentialVault(oauth_settings)
    ts = TokenSet(access_token="a", refresh_token="r", id_token="i", account_id="acct-z")
    vault.store("chatgpt_oauth", "acct-z", ts.to_vault_dict())

    revoke(ts, settings=oauth_settings, vault=vault)

    with pytest.raises(KeyError):
        vault.get("chatgpt_oauth", "acct-z")


# --------------------------------------------------------------------------- #
# device-code flow (headless fallback)
# --------------------------------------------------------------------------- #


def test_device_code_flow_end_to_end(settings, fake_issuer) -> None:
    issuer_base, state = fake_issuer
    state.device_pending_rounds = 2
    oauth_settings = settings.model_copy(update={"chatgpt_oauth_issuer": issuer_base})
    vault = FileCredentialVault(oauth_settings)

    session = start_device_login(oauth_settings)
    assert session.device_code == "devcode-1"
    assert session.user_code == "ABCD-1234"

    token_set = poll_device_login(session, settings=oauth_settings, vault=vault, timeout=10.0)

    assert token_set.account_id == state.account_id
    assert state.device_poll_count == 3  # 2x authorization_pending + 1 success
    stored = vault.get("chatgpt_oauth", state.account_id)
    assert stored["access_token"] == token_set.access_token
