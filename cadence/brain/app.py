"""FastAPI application — the brain's ingest + observability surface.

Endpoints:

* ``POST /ingest/event`` — append-only event intake (mTLS-ready, stubbed in M1). Body
  is a provenance-tagged :class:`~cadence.adapters.base.Event`; routes it through the
  :class:`~cadence.ingest.pipeline.IngestPipeline`.
* ``GET  /healthz``      — liveness.
* ``GET  /metrics``      — Prometheus-format counters (alarms, egress, replication depth).

mTLS is modeled by the :func:`require_mtls` dependency built per-app in
:func:`create_app`. It **fails closed** by default (``settings.require_mtls``): a
request without the ``X-Client-Cert`` header a TLS-terminating proxy would set is
rejected. It is still a stub — only header *presence* is checked, not a real
certificate — but an accidental prod deploy cannot silently accept unauthenticated
callers; set ``require_mtls=False`` (dev/tests only) to open the stub back up.
"""

from __future__ import annotations

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


def create_app(
    *,
    pipeline: IngestPipeline | None = None,
    settings: Settings | None = None,
) -> FastAPI:
    """Application factory. Injecting ``pipeline`` lets tests use an in-memory D1."""
    settings = settings or get_settings()

    def require_mtls(request: Request) -> None:
        """mTLS choke point — STUB, fails closed unless ``settings.require_mtls=False``.

        When mutual TLS is wired, this verifies the client certificate; today it only
        checks for the ``X-Client-Cert`` header a TLS-terminating proxy would set.
        """
        if not settings.require_mtls:
            return None
        if not request.headers.get("x-client-cert"):
            raise HTTPException(status_code=401, detail="mTLS client certificate required")
        return None

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


__all__ = ["create_app"]
