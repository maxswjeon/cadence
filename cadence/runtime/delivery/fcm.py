"""FCM HTTP v1 delivery — real push to a phone (opt-in, credential-gated).

``POST https://fcm.googleapis.com/v1/projects/{project}/messages:send`` with an OAuth2
bearer access token minted from a Firebase **service-account JSON** held NAS-only in the
credential vault (``provider="fcm"``). The message carries a ``notification`` (title +
non-verbatim body) plus a ``data`` block with the ``nudge_id``, category, and the
``Thanks!``/``Dismiss`` actions the client renders — everything the feedback round-trip
needs, and nothing verbatim.

Token minting is **injectable** (``access_token_provider``). The default,
:class:`ServiceAccountTokenSource`, signs a JWT-bearer grant with the service account's
RSA key and exchanges it for an access token — that RSA signing needs the ``cryptography``
package, which is a real runtime dependency of going live (documented in the README). Tests
inject a trivial token provider and point ``fcm_base`` at a fake local server, so the
request *shape* is fully exercised with no Firebase, no real key, and no network egress.

Graceful degradation on the two failures a real deployment hits:

* **401 Unauthorized** — the access token was rejected. The cached token is dropped so the
  next tick re-mints, the failure is logged, and delivery reports ``auth_error`` rather
  than crashing the scheduler.
* **UNREGISTERED / NOT_FOUND** — the device token is dead (app uninstalled / token
  rotated). The dead token is reported via the ``on_unregister`` callback so the caller
  can prune it, and that recipient is skipped.
"""

from __future__ import annotations

import base64
import json
import logging
import threading
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta

import httpx

from cadence.adapters.base import CredentialVault
from cadence.obs.logging import get_logger, log_event
from cadence.runtime.delivery import DeliveryResult, NudgeDelivery, NudgeView

_LOG = get_logger("runtime.delivery.fcm")

#: Vault provider key under which the Firebase service-account JSON is stored.
VAULT_PROVIDER = "fcm"
#: OAuth scope the FCM HTTP v1 send endpoint requires.
_FCM_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
_DEFAULT_TOKEN_URI = "https://oauth2.googleapis.com/token"
_DEFAULT_FCM_BASE = "https://fcm.googleapis.com"
#: Re-mint a little before the real expiry so an in-flight request never uses a just-expired token.
_TOKEN_SKEW_SECONDS = 60


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


class ServiceAccountTokenSource:
    """Mints a Google OAuth2 access token from a service-account JSON (JWT-bearer grant).

    Caches the token until shortly before it expires. Signing the assertion needs the
    ``cryptography`` package (an RSA/RS256 signer); if it is absent, minting raises a clear
    error pointing at the go-live setup rather than failing obscurely at import time.
    """

    def __init__(
        self,
        service_account: dict,
        *,
        scope: str = _FCM_SCOPE,
        client: httpx.Client | None = None,
        timeout: float = 10.0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._sa = service_account
        self._scope = scope
        self._token_uri = service_account.get("token_uri", _DEFAULT_TOKEN_URI)
        self._timeout = timeout
        self._client = client
        self._owns_client = client is None
        self._clock = clock or (lambda: datetime.now(tz=UTC))
        self._lock = threading.Lock()
        self._cached: str | None = None
        self._expires_at: datetime | None = None

    @classmethod
    def from_vault(
        cls, vault: CredentialVault, *, account_ref: str = "default", **kwargs
    ) -> ServiceAccountTokenSource:
        """Load the service-account JSON from the NAS vault (``provider="fcm"``)."""
        return cls(vault.get(VAULT_PROVIDER, account_ref), **kwargs)

    def __call__(self) -> str:
        with self._lock:
            now = self._clock()
            if (
                self._cached is not None
                and self._expires_at is not None
                and now < self._expires_at
            ):
                return self._cached
            token, ttl = self._mint(now)
            self._cached = token
            self._expires_at = now + timedelta(seconds=max(0, ttl - _TOKEN_SKEW_SECONDS))
            return token

    def invalidate(self) -> None:
        """Drop the cached token so the next call re-mints (used after a 401)."""
        with self._lock:
            self._cached = None
            self._expires_at = None

    def _mint(self, now: datetime) -> tuple[str, int]:
        assertion = self._signed_jwt(now)
        try:
            resp = self._http().post(
                self._token_uri,
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                    "assertion": assertion,
                },
            )
        except httpx.HTTPError as exc:
            raise RuntimeError(f"fcm token mint request failed: {exc}") from exc
        if resp.status_code >= 400:
            raise RuntimeError(f"fcm token mint rejected ({resp.status_code})")
        data = resp.json()
        return data["access_token"], int(data.get("expires_in", 3600))

    def _signed_jwt(self, now: datetime) -> str:
        iat = int(now.timestamp())
        header = {"alg": "RS256", "typ": "JWT"}
        claims = {
            "iss": self._sa["client_email"],
            "scope": self._scope,
            "aud": self._token_uri,
            "iat": iat,
            "exp": iat + 3600,
        }
        signing_input = (
            f"{_b64url(json.dumps(header).encode())}.{_b64url(json.dumps(claims).encode())}"
        )
        signature = _rs256_sign(self._sa["private_key"], signing_input.encode("ascii"))
        return f"{signing_input}.{_b64url(signature)}"

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self._timeout)
        return self._client

    def close(self) -> None:
        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None


def _rs256_sign(private_key_pem: str, message: bytes) -> bytes:
    """RS256-sign ``message`` with a PEM private key. Requires ``cryptography``.

    Kept behind a call-time import so this whole module stays importable (and testable
    with an injected token provider) on an install that has not yet added the go-live
    signing dependency.
    """
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
    except ImportError as exc:  # pragma: no cover - go-live dependency
        raise RuntimeError(
            "minting an FCM access token from a service account requires the "
            "'cryptography' package; install it, or inject an access_token_provider"
        ) from exc
    key = serialization.load_pem_private_key(private_key_pem.encode("utf-8"), password=None)
    return key.sign(message, padding.PKCS1v15(), hashes.SHA256())


def _is_unregistered(status_code: int, body: dict) -> bool:
    """True when FCM says the device token is dead (app uninstalled / token rotated)."""
    err = body.get("error") if isinstance(body, dict) else None
    if not isinstance(err, dict):
        return status_code == 404
    if err.get("status") == "NOT_FOUND":
        return True
    for detail in err.get("details") or []:
        if isinstance(detail, dict) and detail.get("errorCode") == "UNREGISTERED":
            return True
    return status_code == 404


class FCMDelivery(NudgeDelivery):
    """Firebase Cloud Messaging HTTP v1 push transport."""

    def __init__(
        self,
        *,
        project_id: str,
        device_tokens: Sequence[str] | Callable[[], Sequence[str]],
        access_token_provider: Callable[[], str],
        fcm_base: str = _DEFAULT_FCM_BASE,
        client: httpx.Client | None = None,
        timeout: float = 10.0,
        on_unregister: Callable[[str], None] | None = None,
    ) -> None:
        self._project_id = project_id
        self._url = f"{fcm_base.rstrip('/')}/v1/projects/{project_id}/messages:send"
        self._device_tokens = device_tokens
        self._access_token_provider = access_token_provider
        self._timeout = timeout
        self._client = client
        self._owns_client = client is None
        self._on_unregister = on_unregister

    def _resolve_tokens(self) -> list[str]:
        tokens = self._device_tokens() if callable(self._device_tokens) else self._device_tokens
        return list(tokens)

    def _message(self, view: NudgeView, device_token: str) -> dict:
        """Build one FCM v1 message (notification + non-verbatim data block)."""
        actions = json.dumps(
            [{"action": a["action"], "title": a["title"]} for a in view.to_payload()["actions"]]
        )
        return {
            "message": {
                "token": device_token,
                "notification": {"title": view.title, "body": view.body},
                "android": {
                    "priority": "high" if view.priority >= 2 else "normal",
                    "notification": {"click_action": "CADENCE_NUDGE"},
                },
                "data": {
                    "nudge_id": view.nudge_id,
                    "category": view.category,
                    "priority": str(view.priority),
                    "actions": actions,
                },
            }
        }

    def _send(self, view: NudgeView) -> DeliveryResult:
        tokens = self._resolve_tokens()
        if not tokens:
            log_event(_LOG, logging.WARNING, "fcm_no_device_tokens", nudge_id=view.nudge_id)
            return DeliveryResult(
                delivered=False, nudge_id=view.nudge_id, reason="no_device_tokens"
            )
        try:
            access_token = self._access_token_provider()
        except Exception as exc:  # noqa: BLE001 - a mint failure must not crash the tick
            log_event(
                _LOG, logging.ERROR, "fcm_token_mint_failed",
                nudge_id=view.nudge_id, error=type(exc).__name__,
            )
            return DeliveryResult(delivered=False, nudge_id=view.nudge_id, reason="auth_error")

        outcomes: dict[str, str] = {}
        auth_failed = False
        for device_token in tokens:
            outcome = self._send_one(view, device_token, access_token)
            outcomes[device_token] = outcome
            if outcome == "auth_error":
                # The token was rejected and invalidated; sending the rest of the batch with
                # the same stale token would just fail identically. Stop and re-mint next tick.
                auth_failed = True
                break
        delivered = any(v == "sent" for v in outcomes.values())
        if delivered:
            reason = "sent"
        elif auth_failed:
            reason = "auth_error"
        else:
            reason = "not_delivered"
        return DeliveryResult(
            delivered=delivered,
            nudge_id=view.nudge_id,
            reason=reason,
            detail={"outcomes": outcomes},
        )

    def _send_one(self, view: NudgeView, device_token: str, access_token: str) -> str:
        try:
            resp = self._http().post(
                self._url,
                json=self._message(view, device_token),
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                },
            )
        except httpx.HTTPError as exc:
            log_event(
                _LOG, logging.ERROR, "fcm_send_failed",
                nudge_id=view.nudge_id, error=type(exc).__name__,
            )
            return "error"

        if resp.status_code == 401:
            # The access token was rejected — drop any cache so the next tick re-mints.
            log_event(_LOG, logging.ERROR, "fcm_auth_rejected", nudge_id=view.nudge_id)
            provider = self._access_token_provider
            if hasattr(provider, "invalidate"):
                provider.invalidate()
            return "auth_error"

        body = _safe_json(resp)
        if resp.status_code >= 400:
            if _is_unregistered(resp.status_code, body):
                log_event(
                    _LOG, logging.INFO, "fcm_device_unregistered", nudge_id=view.nudge_id
                )
                if self._on_unregister is not None:
                    self._on_unregister(device_token)
                return "unregistered"
            log_event(
                _LOG, logging.ERROR, "fcm_send_error",
                nudge_id=view.nudge_id, status=resp.status_code,
            )
            return "error"

        log_event(_LOG, logging.INFO, "fcm_nudge_delivered", nudge_id=view.nudge_id)
        return "sent"

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self._timeout)
        return self._client

    def close(self) -> None:
        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None


def _safe_json(resp: httpx.Response) -> dict:
    try:
        data = resp.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


__all__ = ["FCMDelivery", "ServiceAccountTokenSource", "VAULT_PROVIDER"]
