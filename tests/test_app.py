"""Tests for the FastAPI ingest + observability surface."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from cadence.brain.app import create_app
from cadence.brain.deadlines import RuleDeadlineExtractor
from cadence.ingest.pipeline import IngestPipeline, WALBuffer


@pytest.fixture
def client(store, settings):
    pipeline = IngestPipeline(store)
    app = create_app(pipeline=pipeline, settings=settings)
    with TestClient(app) as c:
        c._pipeline = pipeline  # type: ignore[attr-defined]
        yield c


def _event_body(event_id="e1"):
    return {
        "event_id": event_id,
        "source": "github",
        "account_ref": "octocat",
        "kind": "github.issue",
        "summary": "issue: fix bug",
        "confidence": 0.9,
    }


def test_healthz(client) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}


def test_ingest_event_accepts_and_dedupes(client) -> None:
    r1 = client.post("/ingest/event", json=_event_body())
    assert r1.status_code == 202
    assert r1.json()["accepted"] is True
    assert r1.json()["fact_id"]

    r2 = client.post("/ingest/event", json=_event_body())
    assert r2.status_code == 200
    assert r2.json()["duplicate"] is True


def test_ingest_rejects_extra_raw_field(client) -> None:
    body = _event_body()
    body["raw_body"] = "verbatim raw text"  # extra=forbid on Event
    assert client.post("/ingest/event", json=body).status_code == 422


def test_backpressure_returns_503(store, settings) -> None:
    pipeline = IngestPipeline(store, wal=WALBuffer(max_depth=0))
    app = create_app(pipeline=pipeline, settings=settings)
    with TestClient(app) as c:
        r = c.post("/ingest/event", json=_event_body())
        assert r.status_code == 503
        assert r.json()["error"] == "backpressure"


def test_metrics_endpoint(client) -> None:
    client.post("/ingest/event", json=_event_body())
    r = client.get("/metrics")
    assert r.status_code == 200
    assert "cadence_alarm_total" in r.text
    assert "cadence_replication_queue_depth" in r.text


# --------------------------------------------------------------------------- #
# mTLS fail-closed gate
# --------------------------------------------------------------------------- #


def test_mtls_required_by_default_rejects_missing_cert_header(store, settings) -> None:
    settings = settings.model_copy(update={"require_mtls": True})
    pipeline = IngestPipeline(store)
    app = create_app(pipeline=pipeline, settings=settings)
    with TestClient(app) as c:
        r = c.post("/ingest/event", json=_event_body())
        assert r.status_code == 401


def test_mtls_required_accepts_request_with_cert_header(store, settings) -> None:
    settings = settings.model_copy(update={"require_mtls": True})
    pipeline = IngestPipeline(store)
    app = create_app(pipeline=pipeline, settings=settings)
    with TestClient(app) as c:
        r = c.post(
            "/ingest/event", json=_event_body(), headers={"X-Client-Cert": "dev-fixture-cert"}
        )
        assert r.status_code == 202


def test_mtls_disabled_accepts_request_without_cert_header(client) -> None:
    # The `client` fixture's settings already set require_mtls=False.
    r = client.post("/ingest/event", json=_event_body())
    assert r.status_code == 202


# --------------------------------------------------------------------------- #
# Loopback exemption (same-host pollers ingest without a client cert)
# --------------------------------------------------------------------------- #


def _ingest_client(store, settings, *, client_addr, **overrides):
    """A TestClient whose ASGI peer is ``client_addr`` (drives the loopback check)."""
    settings = settings.model_copy(update={"require_mtls": True, **overrides})
    pipeline = IngestPipeline(store)
    app = create_app(pipeline=pipeline, settings=settings)
    return TestClient(app, client=client_addr)


def test_loopback_no_cert_header_is_allowed(store, settings) -> None:
    with _ingest_client(store, settings, client_addr=("127.0.0.1", 40000)) as c:
        r = c.post("/ingest/event", json=_event_body())
        assert r.status_code == 202


def test_non_loopback_no_cert_header_is_rejected(store, settings) -> None:
    with _ingest_client(store, settings, client_addr=("203.0.113.7", 40000)) as c:
        r = c.post("/ingest/event", json=_event_body())
        assert r.status_code == 401


def test_remote_with_cert_header_uses_existing_path(store, settings) -> None:
    # A proxied remote device still forwards X-Client-Cert -> the cert path, not loopback.
    with _ingest_client(store, settings, client_addr=("203.0.113.7", 40000)) as c:
        r = c.post(
            "/ingest/event", json=_event_body(), headers={"X-Client-Cert": "dev-fixture-cert"}
        )
        assert r.status_code == 202


def test_loopback_exemption_off_rejects_loopback_no_cert(store, settings) -> None:
    with _ingest_client(
        store, settings, client_addr=("127.0.0.1", 40000), trust_loopback_ingest=False
    ) as c:
        r = c.post("/ingest/event", json=_event_body())
        assert r.status_code == 401


# --------------------------------------------------------------------------- #
# Raw-boundary violation -> 422 (not an opaque 500)
# --------------------------------------------------------------------------- #


def test_ingest_raw_boundary_violation_returns_422(client) -> None:
    body = _event_body()
    # A summary over max_summary_len (500, see Settings) trips the raw-boundary
    # classifier on the Fact write — it must surface as a 422, not a bare 500.
    body["summary"] = "x" * 600
    r = client.post("/ingest/event", json=body)
    assert r.status_code == 422
    payload = r.json()
    assert payload["error"] == "raw_boundary_violation"
    assert payload["field"] == "summary"
    assert "reason" in payload


# --------------------------------------------------------------------------- #
# Deadline extractor wired into the production pipeline
# --------------------------------------------------------------------------- #


def test_production_pipeline_uses_rule_deadline_extractor(settings) -> None:
    """The lifespan-built pipeline must not silently drop deadlines (NullDeadlineExtractor)."""
    app = create_app(settings=settings)
    with TestClient(app) as c:
        assert isinstance(c.app.state.pipeline.deadline_extractor, RuleDeadlineExtractor)
        body = _event_body()
        body["structured"] = {"due_at": "2026-07-10T17:00:00Z"}
        r = c.post("/ingest/event", json=body)
        assert r.status_code == 202
        assert r.json()["deadlines_created"] == 1
