"""FastAPI application — the brain's ingest + observability surface.

Endpoints:

* ``POST /ingest/event`` — append-only event intake (mTLS-ready, stubbed in M1). Body
  is a provenance-tagged :class:`~cadence.adapters.base.Event`; routes it through the
  :class:`~cadence.ingest.pipeline.IngestPipeline`.
* ``GET  /healthz``      — liveness.
* ``GET  /metrics``      — Prometheus-format counters (alarms, egress, replication depth).

mTLS is modeled by the shared :func:`enforce_mtls` choke point, wrapped by a per-app
``require_mtls`` dependency in :func:`create_app` (and reused by the feedback route in
:mod:`cadence.runtime.service`). It **fails closed** by default
(``settings.require_mtls``): a request without the ``X-Client-Cert`` header a
TLS-terminating proxy would set is rejected — *unless* it comes from a loopback peer and
``settings.trust_loopback_ingest`` is on, which lets a same-host poller ingest without a
cert (the proxy still forwards the header for remote devices). It is still a stub — only
header *presence* is checked, not a real certificate — but an accidental prod deploy
cannot silently accept unauthenticated off-host callers; set ``require_mtls=False``
(dev/tests only) to open the stub back up.
"""

from __future__ import annotations

import ipaddress
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import PlainTextResponse

from cadence.adapters.base import Event
from cadence.brain.deadlines import RuleDeadlineExtractor
from cadence.config import Settings, get_settings
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


def enforce_mtls(request: Request, settings: Settings) -> None:
    """Shared mTLS choke point — STUB, fails closed unless ``settings.require_mtls=False``.

    Decision order (the ingest and feedback routes both route through here):

    * ``require_mtls`` off → allow (dev/tests where mTLS is not terminated).
    * ``X-Client-Cert`` header present → allow — proxied device traffic on the existing
      cert path (when real mTLS is wired this verifies the client certificate; today it
      only checks header presence).
    * no header, but ``trust_loopback_ingest`` is set and the peer is loopback → allow —
      a same-host poller (see :mod:`cadence.runtime.poller`) that does not proxy through
      the TLS terminator. The peer address is the real socket peer, not a spoofable
      header, so this never opens the gate to off-host callers.
    * otherwise → 401.
    """
    if not settings.require_mtls:
        return None
    if request.headers.get("x-client-cert"):
        return None
    if settings.trust_loopback_ingest and _client_is_loopback(request):
        return None
    raise HTTPException(status_code=401, detail="mTLS client certificate required")


def create_app(
    *,
    pipeline: IngestPipeline | None = None,
    settings: Settings | None = None,
) -> FastAPI:
    """Application factory. Injecting ``pipeline`` lets tests use an in-memory D1."""
    settings = settings or get_settings()

    def require_mtls(request: Request) -> None:
        """Per-app dependency wrapping the shared :func:`enforce_mtls` choke point."""
        enforce_mtls(request, settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if app.state.pipeline is None:
            settings.ensure_dirs()
            store = D1Store(settings)
            store.init_schema()
            app.state.pipeline = IngestPipeline(store, deadline_extractor=RuleDeadlineExtractor())
        yield

    app = FastAPI(title="Cadence brain", version="0.1.0", lifespan=lifespan)
    app.state.pipeline = pipeline

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

    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok"}

    @app.get("/metrics", response_class=PlainTextResponse)
    def metrics() -> str:
        pipe = app.state.pipeline
        store = pipe.d1 if pipe is not None else None
        return render_prometheus(store)

    return app


__all__ = ["create_app", "enforce_mtls"]
