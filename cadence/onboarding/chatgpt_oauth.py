"""ChatGPT-OAuth onboarding — browser PKCE login (+ headless device-code fallback).

.. warning::
   **GRAY ZONE.** This logs a user in the way the Codex CLI does: it presents the
   public Codex ``client_id`` + ``originator`` to the ChatGPT backend and mints an
   OAuth token scoped to the user's own ChatGPT account. It is **unsupported for
   non-Codex use, may break without notice, is ToS-gray, and is subject to
   message-count (not token-count) rate limits.** The fully-supported, recommended
   path is an OpenAI API key (:mod:`cadence.onboarding.api_key`). Every endpoint,
   ``client_id``, ``originator``, and port below is *configuration with a documented
   default* (:class:`cadence.config.Settings`, ``CADENCE_*`` env-overridable) — never a
   silent hardcode. :mod:`cadence.onboarding.__main__` prints this warning and requires
   an explicit ``yes`` before running :meth:`ChatGPTOAuthOnboarding.login`.

Browser flow (Authorization Code + PKCE, S256)
-----------------------------------------------
1. Generate a 64+ char PKCE ``code_verifier`` + ``code_challenge = BASE64URL(SHA256(verifier))``
   and a random ``state``.
2. Start a loopback HTTP listener on ``127.0.0.1:1455`` (fallback ``1457``), path
   ``/auth/callback``.
3. Open the browser at ``{issuer}/oauth/authorize`` with ``response_type=code``,
   ``client_id``, ``redirect_uri``, ``scope``, ``code_challenge``,
   ``code_challenge_method=S256``, ``id_token_add_organizations=true``,
   ``codex_cli_simplified_flow=true``, ``state``, ``originator``.
4. On the callback, verify ``state`` matches (reject on mismatch — CSRF), then exchange
   the ``code`` at ``{issuer}/oauth/token`` (``grant_type=authorization_code`` +
   ``code_verifier``; public client, no secret) for ``{id_token, access_token,
   refresh_token}``.
5. Parse ``chatgpt_account_id`` out of the ``id_token``'s ``https://api.openai.com/auth``
   claim (payload decode only — we never verify the signature; it is a token we just
   minted for ourselves over a connection we made, not an untrusted third-party
   assertion) and persist a :class:`~cadence.llm.provider.TokenSet` to the NAS-only
   vault under ``provider="chatgpt_oauth"``, ``account_ref=<account_id>``.

:func:`refresh` and :func:`revoke` are the shared lifecycle helpers
:class:`cadence.llm.chatgpt_oauth.ChatGPTOAuthProvider` calls back into (via
:func:`make_refresh_callable`) to rotate/revoke the same ``TokenSet``.
:func:`start_device_login` / :func:`poll_device_login` are the headless fallback for
machines with no local browser.

Nothing here ever logs a token, refresh token, or id_token — only account ids/hashes.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import logging
import secrets
import socket
import threading
import time
import urllib.parse
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx

from cadence.adapters.base import CredentialVault
from cadence.adapters.vault import FileCredentialVault
from cadence.config import Settings, get_settings
from cadence.llm.chatgpt_oauth import VAULT_PROVIDER
from cadence.llm.provider import TokenSet
from cadence.obs.logging import get_logger, log_event

#: Loopback redirect path (fixed — matches the Codex CLI's own callback route).
DEFAULT_REDIRECT_PATH = "/auth/callback"

#: The ``https://api.openai.com/auth`` id_token claim carrying ``chatgpt_account_id``.
_AUTH_CLAIM = "https://api.openai.com/auth"

_LOGGER = get_logger("onboarding.chatgpt_oauth")

GRAY_ZONE_WARNING = """\
ChatGPT-OAuth sign-in is a GRAY ZONE — read before continuing.

  * It reaches the ChatGPT backend by presenting the PUBLIC Codex CLI client_id and
    originator, the way the Codex CLI itself does. It is UNSUPPORTED for anything
    other than Codex CLI use.
  * It may break without notice if OpenAI changes the backend.
  * It is ToS-gray, not an officially sanctioned integration.
  * It is rate-limited by message count (not token count), independent of any API
    quota you may separately have.

The fully-supported, recommended path is an OpenAI API key (pay-per-token, no ToS
issue) — see 'python -m cadence.onboarding --provider openai_api'.
"""

_SUCCESS_HTML = """<!doctype html>
<html><head><title>Cadence &mdash; signed in</title></head>
<body><h1>Signed in.</h1><p>You can close this tab and return to the terminal.</p></body>
</html>
"""


# --------------------------------------------------------------------------- #
# PKCE
# --------------------------------------------------------------------------- #


def _new_code_verifier() -> str:
    """A PKCE code verifier: 64+ chars from the RFC 7636 unreserved base64url alphabet."""
    return secrets.token_urlsafe(64)  # ~86 chars — within the RFC's 43-128 char bound


def _code_challenge(verifier: str) -> str:
    """``BASE64URL(SHA256(verifier))``, unpadded, per RFC 7636 S256."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def build_authorize_url(
    settings: Settings, *, redirect_uri: str, code_challenge: str, state: str
) -> str:
    """The ``/oauth/authorize`` URL for the loopback PKCE flow (exact Codex CLI params)."""
    params = {
        "response_type": "code",
        "client_id": settings.chatgpt_oauth_client_id,
        "redirect_uri": redirect_uri,
        "scope": settings.chatgpt_oauth_scopes,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "id_token_add_organizations": "true",
        "codex_cli_simplified_flow": "true",
        "state": state,
        "originator": settings.chatgpt_oauth_originator,
    }
    return f"{settings.chatgpt_oauth_issuer}/oauth/authorize?{urllib.parse.urlencode(params)}"


# --------------------------------------------------------------------------- #
# id_token claim parsing
# --------------------------------------------------------------------------- #


def _b64url_decode_json(segment: str) -> dict:
    padded = segment + "=" * (-len(segment) % 4)
    return json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))


def _account_id_from_id_token(id_token: str) -> str:
    """Pull ``chatgpt_account_id`` out of the id_token's ``https://api.openai.com/auth``
    claim. Payload-only decode — the signature is deliberately never checked (see the
    module docstring)."""
    parts = id_token.split(".")
    if len(parts) < 2:
        raise ValueError("malformed id_token: expected a JWT with a base64url payload segment")
    payload = _b64url_decode_json(parts[1])
    claim = payload.get(_AUTH_CLAIM)
    account_id = claim.get("chatgpt_account_id") if isinstance(claim, dict) else None
    if not account_id:
        raise ValueError(f"id_token is missing the '{_AUTH_CLAIM}.chatgpt_account_id' claim")
    return account_id


def _expires_at_from_payload(payload: dict) -> datetime | None:
    expires_in = payload.get("expires_in")
    if expires_in is None:
        return None
    return datetime.now(tz=UTC) + timedelta(seconds=float(expires_in))


def _exchange_code(
    code: str,
    *,
    code_verifier: str | None,
    redirect_uri: str | None,
    settings: Settings,
) -> TokenSet:
    """``POST {issuer}/oauth/token`` — ``grant_type=authorization_code``. Shared by the
    browser (PKCE) and device-code (no PKCE) flows; both omit the client secret (public
    client)."""
    data: dict[str, str] = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": settings.chatgpt_oauth_client_id,
    }
    if redirect_uri is not None:
        data["redirect_uri"] = redirect_uri
    if code_verifier is not None:
        data["code_verifier"] = code_verifier
    resp = httpx.post(f"{settings.chatgpt_oauth_issuer}/oauth/token", data=data, timeout=30.0)
    resp.raise_for_status()
    payload = resp.json()
    id_token = payload["id_token"]
    return TokenSet(
        access_token=payload["access_token"],
        refresh_token=payload.get("refresh_token"),
        id_token=id_token,
        account_id=_account_id_from_id_token(id_token),
        expires_at=_expires_at_from_payload(payload),
    )


# --------------------------------------------------------------------------- #
# Loopback callback listener
# --------------------------------------------------------------------------- #


class _CallbackResult:
    """Populated once the loopback listener receives the OAuth redirect."""

    def __init__(self) -> None:
        self.params: dict[str, str] | None = None
        self.event = threading.Event()


def _make_handler(result: _CallbackResult) -> type[http.server.BaseHTTPRequestHandler]:
    class _Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            # Silence stderr logging entirely — the query string carries the auth
            # ``code``, which must never hit a log (raw-boundary discipline applies here
            # too, even though this is a local-only listener).
            pass

        def do_GET(self) -> None:  # noqa: N802 - stdlib handler method name
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == DEFAULT_REDIRECT_PATH:
                result.params = dict(urllib.parse.parse_qsl(parsed.query))
                self.send_response(302)
                self.send_header("Location", "/success")
                self.end_headers()
                result.event.set()
            elif parsed.path == "/success":
                body = _SUCCESS_HTML.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

    return _Handler


class _IPv6HTTPServer(http.server.HTTPServer):
    address_family = socket.AF_INET6


class _LoopbackListener:
    """Runs the callback handler on one port across every loopback address family we can
    bind.

    The redirect_uri we send to the authorize endpoint is ``http://localhost:<port>/...``
    (not an IP literal) — the public Codex client's registered redirect is the literal
    host ``localhost``, so we can't just switch to ``127.0.0.1`` without risking a
    mismatch there. But ``localhost`` resolves to the IPv6 loopback (``::1``) first on
    many hosts, and an IPv4-only listener would then have nothing answering on that
    address, silently hanging the flow. So instead we widen our own listener: bind both
    ``127.0.0.1`` and ``::1`` on the same port and let whichever the OS/browser actually
    dials reach us.
    """

    def __init__(self, result: _CallbackResult, port: int) -> None:
        handler = _make_handler(result)
        self.port = port
        self._servers: list[http.server.HTTPServer] = []
        try:
            self._servers.append(http.server.HTTPServer(("127.0.0.1", port), handler))
        except OSError:
            pass
        try:
            self._servers.append(_IPv6HTTPServer(("::1", port), handler))
        except OSError:
            pass
        if not self._servers:
            raise OSError(f"could not bind the OAuth loopback listener on port {port}")
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        for server in self._servers:
            thread = threading.Thread(
                target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True
            )
            thread.start()
            self._threads.append(thread)

    def stop(self) -> None:
        for server in self._servers:
            server.shutdown()
        for thread in self._threads:
            thread.join()
        for server in self._servers:
            server.server_close()


def _start_loopback_server(
    settings: Settings,
) -> tuple[_LoopbackListener, _CallbackResult, int]:
    result = _CallbackResult()
    ports = (settings.chatgpt_oauth_redirect_port, settings.chatgpt_oauth_redirect_port_fallback)
    for port in ports:
        try:
            listener = _LoopbackListener(result, port)
        except OSError:
            continue
        return listener, result, port
    raise OSError(
        "could not bind the OAuth loopback listener on "
        f"{settings.chatgpt_oauth_redirect_port} or its fallback "
        f"{settings.chatgpt_oauth_redirect_port_fallback}"
    )


# --------------------------------------------------------------------------- #
# Browser (PKCE) login
# --------------------------------------------------------------------------- #


class ChatGPTOAuthOnboarding:
    """Drives the browser-based PKCE login and persists the resulting
    :class:`~cadence.llm.provider.TokenSet` to the vault. See the module docstring for
    the gray-zone posture; :mod:`cadence.onboarding.__main__` handles the
    warning+confirmation before calling :meth:`login`.
    """

    def __init__(
        self, settings: Settings | None = None, *, vault: CredentialVault | None = None
    ) -> None:
        self._settings = settings or get_settings()
        self._vault = vault or FileCredentialVault(self._settings)

    def login(self, *, open_browser: bool = True, timeout: float = 300.0) -> TokenSet:
        """Run the full loopback PKCE dance and return the stored :class:`TokenSet`.

        Raises ``TimeoutError`` if the callback never arrives, ``ValueError`` if
        ``state`` doesn't match (rejected as a possible CSRF attempt) or the callback is
        missing ``code``, and ``RuntimeError`` if the authorize step itself reported an
        ``error``.
        """
        verifier = _new_code_verifier()
        challenge = _code_challenge(verifier)
        state = secrets.token_urlsafe(24)

        listener, result, port = _start_loopback_server(self._settings)
        redirect_uri = f"http://localhost:{port}{DEFAULT_REDIRECT_PATH}"
        url = build_authorize_url(
            self._settings, redirect_uri=redirect_uri, code_challenge=challenge, state=state
        )

        listener.start()
        try:
            if open_browser:
                webbrowser.open(url)
            else:
                print(f"Open this URL to sign in: {url}")  # noqa: T201 - deliberate CLI output
            if not result.event.wait(timeout):
                raise TimeoutError("timed out waiting for the OAuth callback")
        finally:
            listener.stop()

        params = result.params or {}
        if params.get("state") != state:
            raise ValueError("OAuth 'state' mismatch on callback — rejecting (possible CSRF)")
        if "error" in params:
            raise RuntimeError(f"OAuth authorize error: {params['error']}")
        if "code" not in params:
            raise ValueError("OAuth callback is missing the 'code' parameter")

        token_set = _exchange_code(
            params["code"],
            code_verifier=verifier,
            redirect_uri=redirect_uri,
            settings=self._settings,
        )
        self._vault.store(VAULT_PROVIDER, token_set.account_id, token_set.to_vault_dict())
        log_event(
            _LOGGER,
            logging.INFO,
            "chatgpt_oauth_login_succeeded",
            account_id=token_set.account_id,
        )
        return token_set


# --------------------------------------------------------------------------- #
# Refresh / revoke — called back into by cadence.llm.chatgpt_oauth.ChatGPTOAuthProvider
# --------------------------------------------------------------------------- #


def refresh(token_set: TokenSet, *, settings: Settings | None = None) -> TokenSet:
    """Rotate ``token_set`` via ``grant_type=refresh_token``.

    Pure network round trip — does **not** touch the vault. This is deliberately the
    exact ``Callable[[TokenSet], TokenSet]`` shape
    :class:`cadence.llm.chatgpt_oauth.ChatGPTOAuthProvider` expects for its
    ``refresh_callable``: the provider persists the rotated tokens itself through its
    own injected vault right after calling this (see :func:`make_refresh_callable` for a
    settings-bound adapter). Onboarding-time (CLI) refresh uses
    :func:`refresh_and_persist` instead, which persists explicitly.
    """
    settings = settings or get_settings()
    if not token_set.refresh_token:
        raise ValueError("token_set has no refresh_token to rotate")
    resp = httpx.post(
        f"{settings.chatgpt_oauth_issuer}/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": token_set.refresh_token,
            "client_id": settings.chatgpt_oauth_client_id,
        },
        timeout=30.0,
    )
    resp.raise_for_status()
    payload = resp.json()
    return TokenSet(
        access_token=payload["access_token"],
        refresh_token=payload.get("refresh_token", token_set.refresh_token),
        id_token=payload.get("id_token", token_set.id_token),
        account_id=token_set.account_id,
        expires_at=_expires_at_from_payload(payload),
    )


def make_refresh_callable(settings: Settings | None = None) -> Callable[[TokenSet], TokenSet]:
    """Bind :func:`refresh` to ``settings``, producing the plain single-argument callable
    ``ChatGPTOAuthProvider(refresh_callable=...)`` expects."""
    bound_settings = settings or get_settings()
    return lambda tokens: refresh(tokens, settings=bound_settings)


def refresh_and_persist(
    token_set: TokenSet,
    *,
    settings: Settings | None = None,
    vault: CredentialVault | None = None,
) -> TokenSet:
    """:func:`refresh`, then re-persist the rotated tokens to the vault (onboarding/CLI
    use; ``ChatGPTOAuthProvider`` persists through its own vault handle instead)."""
    settings = settings or get_settings()
    rotated = refresh(token_set, settings=settings)
    v = vault or FileCredentialVault(settings)
    v.store(VAULT_PROVIDER, rotated.account_id, rotated.to_vault_dict())
    return rotated


def revoke(
    token_set: TokenSet,
    *,
    settings: Settings | None = None,
    vault: CredentialVault | None = None,
) -> None:
    """Revoke ``token_set`` at ``{issuer}/oauth/revoke`` and remove it from the vault.

    Best-effort remote revoke: a network failure still removes the local credential
    (the user's intent — "log out" — is unambiguous either way), but is logged.
    """
    settings = settings or get_settings()
    token = token_set.refresh_token or token_set.access_token
    try:
        resp = httpx.post(
            f"{settings.chatgpt_oauth_issuer}/oauth/revoke",
            data={"client_id": settings.chatgpt_oauth_client_id, "token": token},
            timeout=30.0,
        )
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        log_event(
            _LOGGER,
            logging.WARNING,
            "chatgpt_oauth_revoke_remote_failed",
            account_id=token_set.account_id,
            error=str(exc),
        )
    v = vault or FileCredentialVault(settings)
    if token_set.account_id is not None:
        v.revoke(VAULT_PROVIDER, token_set.account_id)


# --------------------------------------------------------------------------- #
# Device-code flow (headless fallback)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class DeviceAuthSession:
    """A pending device-code login — show ``user_code``/``verification_uri`` to the user,
    then :func:`poll_device_login`."""

    device_code: str
    user_code: str
    verification_uri: str
    interval: float
    expires_in: float


def start_device_login(settings: Settings | None = None) -> DeviceAuthSession:
    """``POST {issuer}/api/accounts/deviceauth/usercode`` — begin the headless device-code
    flow (for machines with no local browser)."""
    settings = settings or get_settings()
    resp = httpx.post(
        f"{settings.chatgpt_oauth_issuer}/api/accounts/deviceauth/usercode",
        json={"client_id": settings.chatgpt_oauth_client_id},
        timeout=30.0,
    )
    resp.raise_for_status()
    data = resp.json()
    return DeviceAuthSession(
        device_code=data["device_code"],
        user_code=data["user_code"],
        verification_uri=(
            data.get("verification_uri") or f"{settings.chatgpt_oauth_issuer}/codex/device"
        ),
        interval=float(data.get("interval", 5)),
        expires_in=float(data.get("expires_in", 900)),
    )


def poll_device_login(
    session: DeviceAuthSession,
    *,
    settings: Settings | None = None,
    vault: CredentialVault | None = None,
    timeout: float | None = None,
) -> TokenSet:
    """Poll ``{issuer}/api/accounts/deviceauth/token`` until the user finishes verifying in
    a browser elsewhere, then exchange the returned authorization ``code`` at
    ``/oauth/token`` (no PKCE here — there is no loopback redirect to bind a verifier to)
    and persist the resulting :class:`TokenSet`."""
    settings = settings or get_settings()
    deadline = time.monotonic() + (timeout if timeout is not None else session.expires_in)
    while True:
        resp = httpx.post(
            f"{settings.chatgpt_oauth_issuer}/api/accounts/deviceauth/token",
            json={
                "client_id": settings.chatgpt_oauth_client_id,
                "device_code": session.device_code,
            },
            timeout=30.0,
        )
        data = resp.json()
        if resp.status_code == 200 and data.get("code"):
            token_set = _exchange_code(
                data["code"], code_verifier=None, redirect_uri=None, settings=settings
            )
            v = vault or FileCredentialVault(settings)
            v.store(VAULT_PROVIDER, token_set.account_id, token_set.to_vault_dict())
            return token_set
        error = data.get("error", "authorization_pending")
        if error != "authorization_pending":
            raise RuntimeError(f"device login failed: {error}")
        if time.monotonic() >= deadline:
            raise TimeoutError("device login timed out waiting for user verification")
        time.sleep(session.interval)


__all__ = [
    "DEFAULT_REDIRECT_PATH",
    "GRAY_ZONE_WARNING",
    "ChatGPTOAuthOnboarding",
    "DeviceAuthSession",
    "build_authorize_url",
    "make_refresh_callable",
    "poll_device_login",
    "refresh",
    "refresh_and_persist",
    "revoke",
    "start_device_login",
]
