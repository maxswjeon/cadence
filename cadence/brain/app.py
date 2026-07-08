"""FastAPI application — the brain's ingest + observability surface.

Endpoints:

* ``POST /ingest/event`` — append-only event intake. Body is a provenance-tagged
  :class:`~cadence.adapters.base.Event`; routes it through the
  :class:`~cadence.ingest.pipeline.IngestPipeline`.
* ``GET  /healthz``      — liveness.
* ``GET  /metrics``      — Prometheus-format counters (alarms, egress, replication depth).

Trust model / mTLS gate
------------------------
The shared :func:`enforce_mtls` choke point (used by the ingest route here and the
feedback route in :mod:`cadence.runtime.service`) **fails closed** by default
(``settings.require_mtls``) and verifies the forwarded client cert with a
:class:`~cadence.devices.verify.DeviceVerifier` (chain-to-CA + accepted-device lookup) —
never a presence-only check.

The subtlety: the app terminates plain HTTP, so a client cert forwarded in the
``X-Client-Cert`` header is **not secret** — a copied cert would let any peer impersonate
a device on a network-exposed bind. Two deployment postures make that safe:

* **Proxy-hop authenticated (recommended, required for network exposure).** Set
  ``proxy_shared_secret`` and have the trusted TLS-terminating proxy — the only component
  that actually verified the cert at the transport layer — inject it as the
  ``X-Cadence-Proxy-Auth`` header. Then ``X-Client-Cert`` is honored **only** on a
  proxy-authenticated request (constant-time secret compare); a cert replayed by any other
  peer is rejected, and a proxy-authenticated request that carries no valid cert is
  rejected (it does **not** fall through to the loopback exemption — that closes a
  misconfigured ``optional`` client-verify proxy).
* **Loopback-only (dev / single box).** With no ``proxy_shared_secret`` configured the
  gate still verifies any presented cert, but a certless request is admitted only from a
  loopback peer (``trust_loopback_ingest``). **HARD REQUIREMENT:** in this posture the API
  MUST bind loopback only and sit behind a same-host proxy — do not expose it on the
  network without setting ``proxy_shared_secret``.

If no verifier is wired (e.g. pure-dev), a present ``X-Client-Cert`` fails closed (401).
Set ``require_mtls=False`` (dev/tests only) to bypass the gate entirely.
"""

from __future__ import annotations

import hmac
import ipaddress
from contextlib import asynccontextmanager
from typing import NoReturn

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import PlainTextResponse

from cadence.adapters.base import Event
from cadence.brain.deadlines import RuleDeadlineExtractor
from cadence.config import Settings, get_settings
from cadence.devices.enrollment import EnrollmentError, EnrollmentRequest, EnrollmentService
from cadence.devices.registry import DeviceRegistry, PendingCapExceeded
from cadence.devices.verify import DeviceVerificationError, DeviceVerifier
from cadence.ingest.pipeline import BackpressureError, IngestPipeline
from cadence.obs.logging import get_logger
from cadence.obs.metrics import render_prometheus
from cadence.stores.d1 import D1Store
from cadence.stores.raw_boundary import RawBoundaryViolation

_log = get_logger("brain.app")


def _client_is_loopback(request: Request) -> bool:
    """True only if the request's TCP peer is a loopback address (127.0.0.0/8, ::1).

    Uses the connection's real peer (``request.client.host``), which is set by the ASGI
    server from the socket and is **not** attacker-controllable via a header — a remote
    caller cannot forge it. A missing/unparseable peer fails closed (not loopback).
    """
    client = request.client
    if client is None:
        return False
    try:
        return ipaddress.ip_address(client.host).is_loopback
    except ValueError:
        return False


#: Header the trusted TLS-terminating proxy injects to prove the hop is authenticated
#: (carrying ``settings.proxy_shared_secret``). Client certs are only honored on requests
#: that carry a matching value — see :func:`enforce_mtls`.
PROXY_AUTH_HEADER = "x-cadence-proxy-auth"

#: Single generic gate-denied response — details are logged server-side, never leaked to
#: the caller (a probing peer learns only "authentication required", not which check failed).
_GATE_DENIED_DETAIL = "mTLS authentication required"


def _deny(reason: str) -> NoReturn:
    """Log the real (server-side) reason and raise a generic 401. Never returns."""
    _log.warning("mTLS gate denied request: %s", reason)
    raise HTTPException(status_code=401, detail=_GATE_DENIED_DETAIL)


def _proxy_authenticated(request: Request, secret: str) -> bool:
    """True iff the request carries the proxy shared secret (constant-time compare)."""
    presented = request.headers.get(PROXY_AUTH_HEADER)
    if not presented:
        return False
    return hmac.compare_digest(presented, secret)


def _require_verified_cert(
    header: str | None, verifier: DeviceVerifier | None
) -> None:
    """Allow only a present, verifiable client cert; otherwise :func:`_deny`.

    Fails closed on a missing cert, a missing verifier, and any verification failure —
    including unexpected errors (the verifier already coerces those into a
    :class:`DeviceVerificationError`, but the broad catch is a second safety net so an
    exotic cert can never surface as a 500 or an accidental allow).
    """
    if not header:
        _deny("proxy-authenticated request carried no client certificate")
    if verifier is None:
        _deny("client certificate presented but no verifier is configured")
    try:
        verifier.verify(header)
    except DeviceVerificationError as exc:
        _deny(f"client certificate rejected: {exc}")
    except Exception as exc:  # noqa: BLE001 - never let an unexpected error open the gate
        _deny(f"client certificate verification raised unexpectedly: {exc!r}")


def enforce_mtls(
    request: Request, settings: Settings, verifier: DeviceVerifier | None = None
) -> None:
    """Shared mTLS choke point — fails closed unless ``settings.require_mtls=False``.

    Used by both the ingest route (here) and the feedback route
    (:mod:`cadence.runtime.service`). See the module docstring for the full trust model.

    * ``require_mtls`` off → allow (dev/tests where mTLS is not terminated).
    * ``proxy_shared_secret`` **set** (proxy-hop authentication):
        - proxy-authenticated (matching ``X-Cadence-Proxy-Auth``) → a verifiable
          ``X-Client-Cert`` is **required** (no loopback fallthrough) — allow on success,
          else 401.
        - not proxy-authenticated but presents ``X-Client-Cert`` → 401 (a copied cert
          replayed by an untrusted peer).
        - not proxy-authenticated, no cert, loopback peer, ``trust_loopback_ingest`` → allow
          (genuinely local same-host poller); otherwise 401.
    * ``proxy_shared_secret`` **unset** (loopback-only posture — the API MUST bind loopback
      behind a same-host proxy):
        - ``X-Client-Cert`` present → verify it (401 on failure / no verifier).
        - no cert + loopback peer + ``trust_loopback_ingest`` → allow.
        - otherwise → 401.

    The loopback decision uses the real socket peer (``request.client.host``), which a
    remote caller cannot forge via a header.
    """
    if not settings.require_mtls:
        return None

    header = request.headers.get("x-client-cert")
    secret = settings.proxy_shared_secret

    if secret:
        if _proxy_authenticated(request, secret):
            # Trusted hop: the proxy vouched for transport. Demand a verified cert and do
            # NOT fall through to the loopback exemption (closes the misconfigured
            # optional-client-verify proxy hole).
            _require_verified_cert(header, verifier)
            return None
        # Untrusted hop: a client cert here is a replay of a non-secret, copyable cert.
        if header:
            _deny("client certificate presented without proxy authentication")
        if settings.trust_loopback_ingest and _client_is_loopback(request):
            return None
        _deny("request is neither proxy-authenticated nor a loopback caller")

    # No proxy secret configured: loopback-only posture.
    if header:
        _require_verified_cert(header, verifier)
        return None
    if settings.trust_loopback_ingest and _client_is_loopback(request):
        return None
    _deny("no client certificate and not a loopback caller")


def build_device_verifier(settings: Settings, store: D1Store) -> DeviceVerifier:
    """Assemble a :class:`DeviceVerifier` from a :class:`CadenceCA` over the NAS vault
    and a :class:`DeviceRegistry` over the given local D1 store.

    The vault import is deferred so the vault/crypto dependency chain is only pulled in
    when real verification is actually wired (i.e. ``require_mtls`` is on).
    """
    from cadence.adapters.vault import FileCredentialVault
    from cadence.devices.ca import CadenceCA

    ca = CadenceCA(FileCredentialVault(settings), settings)
    registry = DeviceRegistry(store, max_pending_devices=settings.max_pending_devices)
    return DeviceVerifier(ca, registry)


def create_app(
    *,
    pipeline: IngestPipeline | None = None,
    settings: Settings | None = None,
    verifier: DeviceVerifier | None = None,
) -> FastAPI:
    """Application factory.

    Injecting ``pipeline`` lets tests use an in-memory D1; injecting ``verifier`` lets
    them drive real cert verification without touching the vault. In production both are
    left ``None`` and built in the lifespan from the configured D1 + CA (the verifier only
    when ``require_mtls`` is on).
    """
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if app.state.pipeline is None:
            settings.ensure_dirs()
            store = D1Store(settings)
            store.init_schema()
            app.state.pipeline = IngestPipeline(store, deadline_extractor=RuleDeadlineExtractor())
        # Wire real cert verification for a production (require_mtls) deploy that did not
        # inject one, over the SAME D1 the pipeline writes to.
        if app.state.verifier is None and settings.require_mtls:
            app.state.verifier = build_device_verifier(settings, app.state.pipeline.d1)
        yield

    app = FastAPI(title="Cadence brain", version="0.1.0", lifespan=lifespan)
    app.state.pipeline = pipeline
    app.state.verifier = verifier

    def require_mtls(request: Request) -> None:
        """Per-app dependency wrapping the shared :func:`enforce_mtls` choke point."""
        enforce_mtls(request, settings, app.state.verifier)

    def get_pipeline() -> IngestPipeline:
        return app.state.pipeline

    @app.post("/ingest/event")
    def ingest_event(
        event: Event,
        response: Response,
        _: None = Depends(require_mtls),
        pipe: IngestPipeline = Depends(get_pipeline),
    ) -> dict:
        try:
            result = pipe.ingest(event)
        except BackpressureError as exc:
            response.status_code = 503
            return {"accepted": False, "error": "backpressure", "detail": str(exc)}
        except RawBoundaryViolation as exc:
            response.status_code = 422
            return {
                "accepted": False,
                "error": "raw_boundary_violation",
                "field": exc.field,
                "reason": exc.reason,
            }
        if result.duplicate:
            response.status_code = 200
        else:
            response.status_code = 202
        return {
            "accepted": result.accepted,
            "duplicate": result.duplicate,
            "dedupe_id": result.dedupe_id,
            "fact_id": result.fact_id,
            "deadlines_created": result.deadlines_created,
            "wal_offset": result.wal_offset,
        }

    @app.post("/device/enroll", status_code=201)
    def device_enroll(
        request: EnrollmentRequest,
        response: Response,
        pipe: IngestPipeline = Depends(get_pipeline),
    ) -> dict:
        """Un-authenticated device enrollment intake — **EXEMPT from enforce_mtls**.

        This is the deliberate exception to the mTLS gate: an un-enrolled device has no
        client certificate yet, so it cannot be behind :func:`require_mtls` (that would
        be a chicken-and-egg deadlock). The exemption is safe because the endpoint is
        powerless — it only ever records a single ``pending``, untrusted device row and
        issues **no** certificate. Trust follows a Trust-On-First-Use + explicit human
        accept model: a human operator reviews and accepts the pending device out of
        band (the operator CLI + cert issuance are B2). See
        :mod:`cadence.devices.enrollment`.

        Idempotent on the key fingerprint, so retries / rate-limited clients re-POSTing
        the same key map to the same row rather than growing the table.
        """
        registry = DeviceRegistry(pipe.d1, max_pending_devices=settings.max_pending_devices)
        service = EnrollmentService(registry)
        try:
            device = service.enroll(request)
        except EnrollmentError as exc:
            response.status_code = 422
            return {"accepted": False, "error": "invalid_enrollment", "detail": str(exc)}
        except PendingCapExceeded as exc:
            # Un-authenticated intake fails closed under storage-amplification pressure.
            response.status_code = 429
            return {"accepted": False, "error": "pending_capacity", "detail": str(exc)}
        # `accepted`/`trusted` are always False here: enrolling never confers trust.
        return {
            "accepted": False,
            "trusted": False,
            "device_id": device.id,
            "status": device.status,
            "public_key_fingerprint": device.public_key_fingerprint,
        }

    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok"}

    @app.get("/metrics", response_class=PlainTextResponse)
    def metrics() -> str:
        pipe = app.state.pipeline
        store = pipe.d1 if pipe is not None else None
        return render_prometheus(store)

    return app


__all__ = ["create_app", "enforce_mtls", "build_device_verifier", "PROXY_AUTH_HEADER"]
