"""End-to-end runtime proof: events -> tick -> live nudge -> delivery -> feedback.

The headline test seeds a real misallocation in D1, drives one deterministic scheduler
tick, asserts a LIVE nudge is produced and handed to a mock delivery with the exact push
payload (title/body + Thanks!/Dismiss + nudge_id + category), then POSTs feedback to the
FastAPI endpoint and asserts the governor recorded it and moved its threshold.

Supporting proofs: a shadow-mode tick proposes but does not deliver; a bad tick (engine
raises) is survived and logged; and the FCM request *shape* is exercised against a FAKE
local server (no Firebase, no real credentials, no live network).
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

from _engine_util import make_deadline, make_task, spread_activity
from fastapi.testclient import TestClient

from cadence.engine.engine import AttentionEngine
from cadence.engine.governor import NudgeGovernor, ProposedNudge
from cadence.obs.logging import get_logger
from cadence.runtime.delivery.fcm import (
    FCMDelivery,
    ServiceAccountTokenSource,
    _is_unregistered,
)
from cadence.runtime.delivery.mock import MockDelivery
from cadence.runtime.scheduler import TickScheduler
from cadence.runtime.service import CadenceRuntime, RuntimeConfig, mutable_device_tokens

NOW = datetime(2026, 7, 7, 15, 0, tzinfo=UTC)


def _seed_misallocation(store) -> None:
    """A user immersed in a low-priority task while a higher one is due sooner."""
    refactor = make_task(
        store, "Refactor legacy billing module", priority=1, source_event_ids=["gh-refactor"]
    )
    report = make_task(
        store, "Ship Q3 investor report", priority=4, source_event_ids=["gh-report"]
    )
    make_deadline(store, refactor.id, NOW + timedelta(days=7))
    make_deadline(store, report.id, NOW + timedelta(days=1))
    spread_activity(store, "billing module refactor", NOW, count=11, step_seconds=120)


# --------------------------------------------------------------------------- #
# Headline: live nudge -> delivered -> feedback adjusts the governor
# --------------------------------------------------------------------------- #


def test_live_tick_delivers_nudge_and_feedback_adjusts_governor(store, settings) -> None:
    _seed_misallocation(store)
    delivery = MockDelivery()
    runtime = CadenceRuntime(
        config=RuntimeConfig(governor_mode="live", interval_seconds=60),
        settings=settings,
        store=store,
        delivery=delivery,
    )

    # One deterministic tick drives the whole loop.
    tick = runtime.scheduler.tick_once(NOW)
    assert tick is not None
    live = [n for n in tick.nudges if n.kind == "attention.misallocation"]
    assert len(live) == 1
    nudge = live[0]
    assert nudge.nudge_id is not None  # persisted -> live

    # It was handed to delivery with the full push payload.
    assert len(delivery.delivered) == 1
    payload = delivery.payloads[0]
    assert payload["nudge_id"] == nudge.nudge_id
    assert payload["category"] == "attention.misallocation"
    assert payload["body"] == nudge.message_summary
    assert payload["title"]  # a non-empty, non-verbatim heading
    action_ids = {a["action"] for a in payload["actions"]}
    assert action_ids == {"thanks", "dismiss"}
    assert [a["title"] for a in payload["actions"]] == ["Thanks!", "Dismiss"]

    # Feedback POST to the endpoint runs record_feedback on the SAME governor.
    category = nudge.kind
    before = runtime.governor._threshold(category)  # noqa: SLF001
    client = TestClient(runtime.app)
    resp = client.post(f"/nudge/{nudge.nudge_id}/feedback", json={"kind": "thanks"})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    # Thanks lowers the category bar; a Feedback row was written.
    after = runtime.governor._threshold(category)  # noqa: SLF001
    assert after < before
    assert runtime.governor.thanks_rate() == 1.0


def test_feedback_unknown_nudge_returns_404(store, settings) -> None:
    runtime = CadenceRuntime(
        config=RuntimeConfig(governor_mode="live"), settings=settings, store=store
    )
    client = TestClient(runtime.app)
    resp = client.post("/nudge/does-not-exist/feedback", json={"kind": "dismiss"})
    assert resp.status_code == 404


def test_feedback_rejects_bad_kind(store, settings) -> None:
    runtime = CadenceRuntime(
        config=RuntimeConfig(governor_mode="live"), settings=settings, store=store
    )
    client = TestClient(runtime.app)
    resp = client.post("/nudge/whatever/feedback", json={"kind": "love-it"})
    assert resp.status_code == 422  # Literal["thanks","dismiss"] validation


# --------------------------------------------------------------------------- #
# Loopback exemption on the feedback route (same choke point as ingest)
# --------------------------------------------------------------------------- #
#
# A request that PASSES the mTLS gate reaches record_feedback and 404s on the unknown
# nudge id; a request BLOCKED by the gate 401s before ever getting there. That 404-vs-401
# split is exactly what distinguishes "allowed" from "rejected" here.


def _feedback_client(store, settings, *, client_addr, **overrides):
    settings = settings.model_copy(update={"require_mtls": True, **overrides})
    runtime = CadenceRuntime(
        config=RuntimeConfig(governor_mode="live"), settings=settings, store=store
    )
    return TestClient(runtime.app, client=client_addr)


def test_feedback_loopback_no_cert_header_is_allowed(store, settings) -> None:
    c = _feedback_client(store, settings, client_addr=("127.0.0.1", 40000))
    resp = c.post("/nudge/does-not-exist/feedback", json={"kind": "thanks"})
    assert resp.status_code == 404  # passed the gate, unknown nudge


def test_feedback_non_loopback_no_cert_header_is_rejected(store, settings) -> None:
    c = _feedback_client(store, settings, client_addr=("203.0.113.7", 40000))
    resp = c.post("/nudge/does-not-exist/feedback", json={"kind": "thanks"})
    assert resp.status_code == 401


def test_feedback_remote_with_verified_cert_uses_cert_path(store, settings) -> None:
    # A remote device's accepted, CA-issued cert is really verified on the feedback route
    # too (same DeviceVerifier as ingest); it passes the gate and 404s on the unknown nudge.
    from _mtls_util import build_trust_fabric, new_key

    settings = settings.model_copy(update={"require_mtls": True})
    fabric = build_trust_fabric(store, settings)
    _, cert_pem = fabric.accepted_device_cert(new_key())
    runtime = CadenceRuntime(
        config=RuntimeConfig(governor_mode="live"),
        settings=settings,
        store=store,
        device_verifier=fabric.verifier,
    )
    c = TestClient(runtime.app, client=("203.0.113.7", 40000))
    resp = c.post(
        "/nudge/does-not-exist/feedback",
        json={"kind": "thanks"},
        headers={"X-Client-Cert": cert_pem},
    )
    assert resp.status_code == 404  # cert verified, gate passed, unknown nudge


def test_feedback_unverifiable_cert_header_rejected(store, settings) -> None:
    # The presence-only stub is gone: a bogus header no longer passes the feedback gate.
    from _mtls_util import build_trust_fabric

    settings = settings.model_copy(update={"require_mtls": True})
    fabric = build_trust_fabric(store, settings)
    runtime = CadenceRuntime(
        config=RuntimeConfig(governor_mode="live"),
        settings=settings,
        store=store,
        device_verifier=fabric.verifier,
    )
    c = TestClient(runtime.app, client=("203.0.113.7", 40000))
    resp = c.post(
        "/nudge/does-not-exist/feedback",
        json={"kind": "thanks"},
        headers={"X-Client-Cert": "dev-fixture-cert"},
    )
    assert resp.status_code == 401


def test_feedback_loopback_exemption_off_rejects_loopback_no_cert(store, settings) -> None:
    c = _feedback_client(
        store, settings, client_addr=("127.0.0.1", 40000), trust_loopback_ingest=False
    )
    resp = c.post("/nudge/does-not-exist/feedback", json={"kind": "thanks"})
    assert resp.status_code == 401


# --------------------------------------------------------------------------- #
# Shadow: proposed, never delivered
# --------------------------------------------------------------------------- #


def test_shadow_tick_proposes_but_does_not_deliver(store) -> None:
    _seed_misallocation(store)
    governor = NudgeGovernor(store, mode="shadow")
    engine = AttentionEngine(store, governor)
    delivery = MockDelivery()
    scheduler = TickScheduler(engine, delivery, interval_seconds=60)

    tick = scheduler.tick_once(NOW)

    assert tick is not None
    assert governor.proposals  # the governor did propose a nudge
    assert all(n.nudge_id is None for n in tick.nudges)  # but none are live
    assert delivery.delivered == []  # so nothing was delivered


# --------------------------------------------------------------------------- #
# Resilience: a bad tick is survived and logged
# --------------------------------------------------------------------------- #


class _ExplodingEngine:
    def evaluate(self, now=None):  # noqa: ARG002
        raise RuntimeError("boom in the engine")


def test_bad_tick_is_survived_and_logged(store) -> None:
    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Capture()
    logger = get_logger("runtime.scheduler")
    # Pin local logger state so this assertion is independent of global side-effects other
    # tests leave behind — notably alembic's fileConfig(disable_existing_loggers=True) in
    # the migrations test, which flips ``disabled`` True on every pre-existing cadence logger.
    prev_level, prev_disabled = logger.level, logger.disabled
    logger.setLevel(logging.DEBUG)
    logger.disabled = False
    logger.addHandler(handler)
    try:
        scheduler = TickScheduler(_ExplodingEngine(), MockDelivery(), interval_seconds=60)
        result = scheduler.tick_once(NOW)  # must NOT raise
    finally:
        logger.removeHandler(handler)
        logger.setLevel(prev_level)
        logger.disabled = prev_disabled

    assert result is None
    assert any(r.getMessage() == "tick_evaluate_failed" for r in records)


def test_scheduler_start_stop_lifecycle(store) -> None:
    _seed_misallocation(store)
    engine = AttentionEngine(store, NudgeGovernor(store, mode="live"))
    delivery = MockDelivery()
    # A tiny interval + injected fixed clock keeps the running loop deterministic.
    scheduler = TickScheduler(
        engine, delivery, interval_seconds=0.01, clock=lambda: NOW
    )
    scheduler.start()
    try:
        # Give the loop a moment to fire at least one tick.
        for _ in range(200):
            if delivery.delivered:
                break
            threading.Event().wait(0.005)
    finally:
        scheduler.stop()
    assert not scheduler.running
    assert delivery.delivered  # the running loop delivered the live nudge (idempotent after)


# --------------------------------------------------------------------------- #
# FCM: request shape against a FAKE local server (no Firebase, no real creds)
# --------------------------------------------------------------------------- #


class _FakeFCMServer:
    """Records requests and returns programmed responses (stdlib http.server)."""

    def __init__(self, responder) -> None:
        self.requests: list[tuple[str, dict[str, str], dict]] = []
        server_self = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # silence
                pass

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length) if length else b"{}"
                body = json.loads(raw or b"{}")
                server_self.requests.append((self.path, dict(self.headers.items()), body))
                status, payload = responder(self.path, body)
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self._httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    def __enter__(self) -> _FakeFCMServer:
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"


def _live_nudge() -> ProposedNudge:
    return ProposedNudge(
        idempotency_key="idem-1",
        kind="device.care",
        priority=1,
        message_summary="Battery low (7%) on phone — consider charging.",
        confidence=1.0,
        nudge_id="nudge-abc",
    )


def test_fcm_request_shape_and_auth(store) -> None:
    with _FakeFCMServer(lambda p, b: (200, {"name": "projects/proj-1/messages/1"})) as srv:
        delivery = FCMDelivery(
            project_id="proj-1",
            device_tokens=["device-token-1"],
            access_token_provider=lambda: "fake-access-token",
            fcm_base=srv.base,
        )
        result = delivery.deliver(_live_nudge())
        delivery.close()

    assert result.delivered is True
    path, headers, body = srv.requests[0]
    assert path == "/v1/projects/proj-1/messages:send"
    assert headers["Authorization"] == "Bearer fake-access-token"
    msg = body["message"]
    assert msg["token"] == "device-token-1"
    assert msg["notification"]["title"]
    assert msg["notification"]["body"] == "Battery low (7%) on phone — consider charging."
    assert msg["data"]["nudge_id"] == "nudge-abc"
    assert msg["data"]["category"] == "device.care"
    actions = json.loads(msg["data"]["actions"])
    assert {a["action"] for a in actions} == {"thanks", "dismiss"}


def test_fcm_shadow_nudge_is_never_sent(store) -> None:
    with _FakeFCMServer(lambda p, b: (200, {})) as srv:
        delivery = FCMDelivery(
            project_id="proj-1",
            device_tokens=["device-token-1"],
            access_token_provider=lambda: "fake-access-token",
            fcm_base=srv.base,
        )
        shadow = ProposedNudge(
            idempotency_key="idem-2", kind="device.care", priority=1,
            message_summary="x", confidence=1.0,  # nudge_id is None -> shadow
        )
        result = delivery.deliver(shadow)
        delivery.close()

    assert result.delivered is False
    assert result.reason == "shadow"
    assert srv.requests == []  # nothing ever hit the wire


def test_fcm_401_reports_auth_error_gracefully(store) -> None:
    with _FakeFCMServer(lambda p, b: (401, {"error": {"status": "UNAUTHENTICATED"}})) as srv:
        delivery = FCMDelivery(
            project_id="proj-1",
            device_tokens=["device-token-1"],
            access_token_provider=lambda: "expired-token",
            fcm_base=srv.base,
        )
        result = delivery.deliver(_live_nudge())
        delivery.close()

    assert result.delivered is False
    assert result.detail["outcomes"]["device-token-1"] == "auth_error"


def test_fcm_unregistered_token_is_pruned(store) -> None:
    pruned: list[str] = []

    def responder(path, body):
        return 404, {
            "error": {
                "status": "NOT_FOUND",
                "details": [{"errorCode": "UNREGISTERED"}],
            }
        }

    with _FakeFCMServer(responder) as srv:
        delivery = FCMDelivery(
            project_id="proj-1",
            device_tokens=["dead-token"],
            access_token_provider=lambda: "tok",
            fcm_base=srv.base,
            on_unregister=pruned.append,
        )
        result = delivery.deliver(_live_nudge())
        delivery.close()

    assert result.delivered is False
    assert pruned == ["dead-token"]
    assert result.detail["outcomes"]["dead-token"] == "unregistered"


def test_fcm_is_unregistered_detection() -> None:
    assert _is_unregistered(404, {"error": {"status": "NOT_FOUND"}}) is True
    assert _is_unregistered(400, {"error": {"details": [{"errorCode": "UNREGISTERED"}]}}) is True
    assert _is_unregistered(500, {"error": {"status": "INTERNAL"}}) is False


def test_service_account_token_source_caches_and_invalidates() -> None:
    clock_now = {"t": datetime(2026, 7, 7, 12, 0, tzinfo=UTC)}
    mints: list[int] = []

    src = ServiceAccountTokenSource(
        {"client_email": "svc@x", "private_key": "PEM", "token_uri": "https://x/token"},
        clock=lambda: clock_now["t"],
    )

    def fake_mint(now):
        mints.append(1)
        return f"tok-{len(mints)}", 3600

    src._mint = fake_mint  # noqa: SLF001 - avoid real RSA signing / network

    assert src() == "tok-1"
    assert src() == "tok-1"  # cached, no second mint
    assert len(mints) == 1

    src.invalidate()
    assert src() == "tok-2"  # re-minted after invalidation
    assert len(mints) == 2


# --------------------------------------------------------------------------- #
# M5 hardening: scheduler stop() must not orphan a running tick into a 2nd loop
# --------------------------------------------------------------------------- #


class _BlockingEngine:
    """Engine whose tick blocks until released — models a slow in-flight tick."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def evaluate(self, now=None):  # noqa: ARG002
        self.calls += 1
        self.entered.set()
        self.release.wait(5)
        return SimpleNamespace(nudges=[])


def test_stop_during_blocked_tick_prevents_second_loop() -> None:
    engine = _BlockingEngine()
    scheduler = TickScheduler(
        engine, MockDelivery(), interval_seconds=0.01, clock=lambda: NOW
    )
    scheduler.start()
    assert engine.entered.wait(2)  # the loop is inside a blocking tick
    first_thread = scheduler._thread  # noqa: SLF001

    scheduler.stop(timeout=0.1)  # join times out — the tick is still blocked
    # The still-alive handle is KEPT, not dropped.
    assert scheduler._thread is first_thread  # noqa: SLF001
    assert first_thread.is_alive()

    scheduler.start()  # must be a no-op: no second loop alongside the first
    assert scheduler._thread is first_thread  # noqa: SLF001

    engine.release.set()  # let the blocked tick finish
    scheduler.stop(timeout=2)  # now it joins cleanly
    assert not scheduler.running


# --------------------------------------------------------------------------- #
# M5 hardening: runtime wires UNREGISTERED pruning into the FCM token pool
# --------------------------------------------------------------------------- #


def test_runtime_fcm_pool_prunes_unregistered_token() -> None:
    get_tokens, prune = mutable_device_tokens(["dead-token", "good-token"])

    def responder(path, body):
        if body["message"]["token"] == "dead-token":
            return 404, {
                "error": {"status": "NOT_FOUND", "details": [{"errorCode": "UNREGISTERED"}]}
            }
        return 200, {"name": "ok"}

    with _FakeFCMServer(responder) as srv:
        delivery = FCMDelivery(
            project_id="proj-1",
            device_tokens=get_tokens,
            access_token_provider=lambda: "tok",
            fcm_base=srv.base,
            on_unregister=prune,
        )
        first = delivery.deliver(_live_nudge())
        # The dead token was pruned from the shared pool, so the next tick won't retry it.
        assert get_tokens() == ["good-token"]
        second_requests_before = len(srv.requests)
        delivery.deliver(_live_nudge())
        delivery.close()

    assert first.detail["outcomes"]["dead-token"] == "unregistered"
    assert first.detail["outcomes"]["good-token"] == "sent"
    # Second delivery hit only the surviving token (one more request, not two).
    assert len(srv.requests) - second_requests_before == 1


# --------------------------------------------------------------------------- #
# M5 hardening: FCM breaks the batch on the first auth_error (no stale-token reuse)
# --------------------------------------------------------------------------- #


def test_fcm_breaks_batch_on_auth_error() -> None:
    with _FakeFCMServer(lambda p, b: (401, {"error": {"status": "UNAUTHENTICATED"}})) as srv:
        delivery = FCMDelivery(
            project_id="proj-1",
            device_tokens=["tok-a", "tok-b", "tok-c"],
            access_token_provider=lambda: "expired",
            fcm_base=srv.base,
        )
        result = delivery.deliver(_live_nudge())
        delivery.close()

    # Only the first recipient was attempted — the invalidated token is not reused.
    assert len(srv.requests) == 1
    assert result.delivered is False
    assert result.reason == "auth_error"
    assert list(result.detail["outcomes"]) == ["tok-a"]


# --------------------------------------------------------------------------- #
# M5 hardening: from_env names the offending variable on bad input
# --------------------------------------------------------------------------- #


def test_from_env_reports_bad_interval_and_mode() -> None:
    import pytest

    with pytest.raises(ValueError, match="CADENCE_TICK_INTERVAL_SECONDS"):
        RuntimeConfig.from_env({"CADENCE_TICK_INTERVAL_SECONDS": "not-a-number"})
    with pytest.raises(ValueError, match="CADENCE_TICK_INTERVAL_SECONDS"):
        RuntimeConfig.from_env({"CADENCE_TICK_INTERVAL_SECONDS": "-5"})
    with pytest.raises(ValueError, match="CADENCE_GOVERNOR_MODE"):
        RuntimeConfig.from_env({"CADENCE_GOVERNOR_MODE": "bogus"})
