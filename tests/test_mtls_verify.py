"""Tests for the M8/B2 mTLS device-verification middleware (:mod:`cadence.devices.verify`).

Two layers:

* Unit tests drive :meth:`DeviceVerifier.verify` directly across every accept/reject path.
* Integration tests drive the wired gate on BOTH the ingest route (``/ingest/event``) and
  the feedback route (``/nudge/{id}/feedback``), asserting 401-vs-allowed end to end.
"""

from __future__ import annotations

import pytest
from _mtls_util import (
    build_trust_fabric,
    escaped_pem,
    expired_ca_issued_cert,
    new_key,
    self_signed_cert,
)
from fastapi import HTTPException, Request
from fastapi.testclient import TestClient

from cadence.brain.app import PROXY_AUTH_HEADER, create_app, enforce_mtls
from cadence.devices.verify import DeviceIdentity, DeviceVerificationError
from cadence.ingest.pipeline import IngestPipeline
from cadence.runtime.service import CadenceRuntime, RuntimeConfig

PROXY_SECRET = "s3cret-proxy-token-for-tests"


def _event_body(event_id="e1"):
    return {
        "event_id": event_id,
        "source": "github",
        "account_ref": "octocat",
        "kind": "github.issue",
        "summary": "issue: fix bug",
        "confidence": 0.9,
    }


@pytest.fixture
def fabric(store, settings):
    """A real CA + registry + verifier over the shared in-memory D1."""
    return build_trust_fabric(store, settings)


# --------------------------------------------------------------------------- #
# DeviceVerifier.verify — accept path
# --------------------------------------------------------------------------- #


def test_accepted_device_cert_returns_identity(fabric) -> None:
    key = new_key()
    device, cert_pem = fabric.accepted_device_cert(key, name="phone")
    identity = fabric.verifier.verify(cert_pem)
    assert isinstance(identity, DeviceIdentity)
    assert identity.id == device.id
    assert identity.name == "phone"


def test_accepted_device_url_encoded_header_verifies(fabric) -> None:
    # The proxy forwards URL-encoded PEM (nginx $ssl_client_escaped_cert); it must decode.
    key = new_key()
    device, cert_pem = fabric.accepted_device_cert(key)
    identity = fabric.verifier.verify(escaped_pem(cert_pem))
    assert identity.id == device.id


# --------------------------------------------------------------------------- #
# DeviceVerifier.verify — registry-status rejections
# --------------------------------------------------------------------------- #


def test_revoked_device_cert_rejected(fabric) -> None:
    key = new_key()
    device, cert_pem = fabric.accepted_device_cert(key)
    fabric.registry.revoke(device.id)
    with pytest.raises(DeviceVerificationError, match="not accepted"):
        fabric.verifier.verify(cert_pem)


def test_pending_device_cert_rejected(fabric) -> None:
    # Device enrolled + cert issued, but never accepted (still pending) -> reject.
    key = new_key()
    fabric.enroll(key)
    cert_pem = fabric.issued_cert(key)
    with pytest.raises(DeviceVerificationError, match="not accepted"):
        fabric.verifier.verify(cert_pem)


def test_unknown_device_cert_rejected(fabric) -> None:
    # A genuinely CA-issued cert whose key was never enrolled -> no device row.
    key = new_key()
    cert_pem = fabric.issued_cert(key)
    with pytest.raises(DeviceVerificationError, match="no enrolled device"):
        fabric.verifier.verify(cert_pem)


def test_cert_fingerprint_mismatch_rejected(fabric) -> None:
    # Device accepted on cert A; a DIFFERENT cert (re-issued for the same key, new serial)
    # must NOT authenticate — only the recorded accepted cert does.
    key = new_key()
    fabric.accepted_device_cert(key)  # binds cert A
    other_cert = fabric.issued_cert(key)  # cert B: same key, different fingerprint
    with pytest.raises(DeviceVerificationError, match="fingerprint does not match"):
        fabric.verifier.verify(other_cert)


# --------------------------------------------------------------------------- #
# DeviceVerifier.verify — chain / shape rejections
# --------------------------------------------------------------------------- #


def test_self_signed_foreign_cert_rejected(fabric) -> None:
    # Even after enrolling+accepting the key, a self-signed (not CA-issued) cert fails
    # the chain check before any registry lookup.
    key = new_key()
    fabric.accepted_device_cert(key)
    with pytest.raises(DeviceVerificationError, match="Cadence CA|signature"):
        fabric.verifier.verify(self_signed_cert(key))


def test_expired_ca_issued_cert_rejected(fabric) -> None:
    key = new_key()
    fabric.accepted_device_cert(key)
    with pytest.raises(DeviceVerificationError, match="expired or not yet valid"):
        fabric.verifier.verify(expired_ca_issued_cert(fabric, key))


def test_ca_cert_cannot_authenticate(fabric) -> None:
    # The root CA cert itself chains + verifies but is CA:TRUE -> not a device identity.
    with pytest.raises(DeviceVerificationError, match="CA certificate cannot"):
        fabric.verifier.verify(fabric.ca.ca_cert_pem())


def test_server_auth_only_cert_rejected(fabric) -> None:
    # A CA-issued serverAuth cert (wrong EKU) is not a client identity.
    key = new_key()
    server_pem = fabric.ca.issue_server_cert(key.public_key(), "brain.local")
    with pytest.raises(DeviceVerificationError, match="clientAuth"):
        fabric.verifier.verify(server_pem)


def test_malformed_header_rejected(fabric) -> None:
    with pytest.raises(DeviceVerificationError, match="parseable PEM"):
        fabric.verifier.verify("not-a-certificate")


def test_empty_header_rejected(fabric) -> None:
    with pytest.raises(DeviceVerificationError, match="empty"):
        fabric.verifier.verify("")


# --------------------------------------------------------------------------- #
# enforce_mtls — fail-closed when no verifier is configured
# --------------------------------------------------------------------------- #


def _request_with_header(value: str | None) -> Request:
    headers = [(b"x-client-cert", value.encode())] if value is not None else []
    scope = {
        "type": "http",
        "headers": headers,
        "client": ("203.0.113.7", 40000),
    }
    return Request(scope)


def test_enforce_mtls_present_cert_without_verifier_fails_closed(settings) -> None:
    settings = settings.model_copy(update={"require_mtls": True})
    # A present cert header + NO verifier available must 401 (not fall back to a presence
    # pass). This is the pure-dev / mis-wired posture.
    with pytest.raises(HTTPException) as exc:
        enforce_mtls(_request_with_header("some-cert-bytes"), settings, None)
    assert exc.value.status_code == 401


def test_enforce_mtls_off_allows_without_verifier(settings) -> None:
    settings = settings.model_copy(update={"require_mtls": False})
    assert enforce_mtls(_request_with_header("anything"), settings, None) is None


# --------------------------------------------------------------------------- #
# Ingest route — real verification end to end
# --------------------------------------------------------------------------- #


def _ingest_app(store, settings, fabric, *, client_addr=None):
    settings = settings.model_copy(update={"require_mtls": True})
    pipeline = IngestPipeline(store)
    app = create_app(pipeline=pipeline, settings=settings, verifier=fabric.verifier)
    return TestClient(app, client=client_addr) if client_addr else TestClient(app)


def test_ingest_accepted_cert_allowed(store, settings, fabric) -> None:
    _, cert_pem = fabric.accepted_device_cert(new_key())
    with _ingest_app(store, settings, fabric, client_addr=("203.0.113.7", 40000)) as c:
        r = c.post("/ingest/event", json=_event_body(), headers={"X-Client-Cert": cert_pem})
        assert r.status_code == 202


def test_ingest_revoked_cert_rejected(store, settings, fabric) -> None:
    device, cert_pem = fabric.accepted_device_cert(new_key())
    fabric.registry.revoke(device.id)
    with _ingest_app(store, settings, fabric, client_addr=("203.0.113.7", 40000)) as c:
        r = c.post("/ingest/event", json=_event_body(), headers={"X-Client-Cert": cert_pem})
        assert r.status_code == 401


def test_ingest_pending_cert_rejected(store, settings, fabric) -> None:
    key = new_key()
    fabric.enroll(key)
    cert_pem = fabric.issued_cert(key)
    with _ingest_app(store, settings, fabric, client_addr=("203.0.113.7", 40000)) as c:
        r = c.post("/ingest/event", json=_event_body(), headers={"X-Client-Cert": cert_pem})
        assert r.status_code == 401


def test_ingest_foreign_cert_rejected(store, settings, fabric) -> None:
    key = new_key()
    fabric.accepted_device_cert(key)
    with _ingest_app(store, settings, fabric, client_addr=("203.0.113.7", 40000)) as c:
        r = c.post(
            "/ingest/event",
            json=_event_body(),
            headers={"X-Client-Cert": self_signed_cert(key)},
        )
        assert r.status_code == 401


def test_ingest_expired_cert_rejected(store, settings, fabric) -> None:
    key = new_key()
    fabric.accepted_device_cert(key)
    with _ingest_app(store, settings, fabric, client_addr=("203.0.113.7", 40000)) as c:
        r = c.post(
            "/ingest/event",
            json=_event_body(),
            headers={"X-Client-Cert": expired_ca_issued_cert(fabric, key)},
        )
        assert r.status_code == 401


def test_ingest_fingerprint_mismatch_rejected(store, settings, fabric) -> None:
    key = new_key()
    fabric.accepted_device_cert(key)
    other_cert = fabric.issued_cert(key)  # not the accepted cert
    with _ingest_app(store, settings, fabric, client_addr=("203.0.113.7", 40000)) as c:
        r = c.post("/ingest/event", json=_event_body(), headers={"X-Client-Cert": other_cert})
        assert r.status_code == 401


def test_ingest_malformed_header_rejected(store, settings, fabric) -> None:
    with _ingest_app(store, settings, fabric, client_addr=("203.0.113.7", 40000)) as c:
        r = c.post("/ingest/event", json=_event_body(), headers={"X-Client-Cert": "garbage"})
        assert r.status_code == 401


def test_ingest_loopback_no_header_still_allowed(store, settings, fabric) -> None:
    with _ingest_app(store, settings, fabric, client_addr=("127.0.0.1", 40000)) as c:
        r = c.post("/ingest/event", json=_event_body())
        assert r.status_code == 202


def test_ingest_non_loopback_no_header_rejected(store, settings, fabric) -> None:
    with _ingest_app(store, settings, fabric, client_addr=("203.0.113.7", 40000)) as c:
        r = c.post("/ingest/event", json=_event_body())
        assert r.status_code == 401


# --------------------------------------------------------------------------- #
# Feedback route — same choke point, same DeviceVerifier
# --------------------------------------------------------------------------- #
#
# A request that PASSES the gate reaches record_feedback and 404s on the unknown nudge id;
# a BLOCKED request 401s before getting there. That 404-vs-401 split is allowed-vs-rejected.


def _feedback_client(store, settings, fabric, *, client_addr):
    settings = settings.model_copy(update={"require_mtls": True})
    runtime = CadenceRuntime(
        config=RuntimeConfig(governor_mode="live"),
        settings=settings,
        store=store,
        device_verifier=fabric.verifier,
    )
    return TestClient(runtime.app, client=client_addr)


def test_feedback_accepted_cert_allowed(store, settings, fabric) -> None:
    _, cert_pem = fabric.accepted_device_cert(new_key())
    c = _feedback_client(store, settings, fabric, client_addr=("203.0.113.7", 40000))
    r = c.post(
        "/nudge/does-not-exist/feedback",
        json={"kind": "thanks"},
        headers={"X-Client-Cert": cert_pem},
    )
    assert r.status_code == 404  # cert verified, gate passed, unknown nudge


def test_feedback_revoked_cert_rejected(store, settings, fabric) -> None:
    device, cert_pem = fabric.accepted_device_cert(new_key())
    fabric.registry.revoke(device.id)
    c = _feedback_client(store, settings, fabric, client_addr=("203.0.113.7", 40000))
    r = c.post(
        "/nudge/does-not-exist/feedback",
        json={"kind": "thanks"},
        headers={"X-Client-Cert": cert_pem},
    )
    assert r.status_code == 401


def test_feedback_foreign_cert_rejected(store, settings, fabric) -> None:
    key = new_key()
    fabric.accepted_device_cert(key)
    c = _feedback_client(store, settings, fabric, client_addr=("203.0.113.7", 40000))
    r = c.post(
        "/nudge/does-not-exist/feedback",
        json={"kind": "thanks"},
        headers={"X-Client-Cert": self_signed_cert(key)},
    )
    assert r.status_code == 401


def test_feedback_loopback_no_header_allowed(store, settings, fabric) -> None:
    c = _feedback_client(store, settings, fabric, client_addr=("127.0.0.1", 40000))
    r = c.post("/nudge/does-not-exist/feedback", json={"kind": "thanks"})
    assert r.status_code == 404  # loopback exemption -> gate passed, unknown nudge


def test_feedback_no_verifier_present_cert_fails_closed(store, settings) -> None:
    # require_mtls on but NO verifier wired (device_verifier=None, and the runtime builds
    # one only from a real vault). We force the None posture and assert fail-closed.
    settings = settings.model_copy(update={"require_mtls": True})
    runtime = CadenceRuntime(
        config=RuntimeConfig(governor_mode="live"),
        settings=settings,
        store=store,
        device_verifier=None,
    )
    # Neutralize any verifier the runtime built so we exercise the no-verifier path.
    runtime.app.state.verifier = None
    c = TestClient(runtime.app, client=("203.0.113.7", 40000))
    r = c.post(
        "/nudge/does-not-exist/feedback",
        json={"kind": "thanks"},
        headers={"X-Client-Cert": "anything"},
    )
    assert r.status_code == 401


# --------------------------------------------------------------------------- #
# Proxy-hop authentication (proxy_shared_secret) — X-Client-Cert is only trusted
# on a request the trusted proxy authenticated; a copied cert from a random peer 401s.
# --------------------------------------------------------------------------- #


def _proxy_ingest_app(store, settings, fabric, *, client_addr, secret=PROXY_SECRET):
    settings = settings.model_copy(
        update={"require_mtls": True, "proxy_shared_secret": secret}
    )
    app = create_app(pipeline=IngestPipeline(store), settings=settings, verifier=fabric.verifier)
    return TestClient(app, client=client_addr)


def test_proxy_authenticated_cert_allowed(store, settings, fabric) -> None:
    # Correct cert + proxy secret from a remote peer -> allowed.
    _, cert_pem = fabric.accepted_device_cert(new_key())
    with _proxy_ingest_app(store, settings, fabric, client_addr=("203.0.113.7", 40000)) as c:
        r = c.post(
            "/ingest/event",
            json=_event_body(),
            headers={"X-Client-Cert": cert_pem, PROXY_AUTH_HEADER: PROXY_SECRET},
        )
        assert r.status_code == 202


def test_proxy_copied_cert_without_secret_rejected(store, settings, fabric) -> None:
    # A valid, accepted cert replayed by a peer that lacks the proxy secret -> 401.
    _, cert_pem = fabric.accepted_device_cert(new_key())
    with _proxy_ingest_app(store, settings, fabric, client_addr=("203.0.113.7", 40000)) as c:
        r = c.post("/ingest/event", json=_event_body(), headers={"X-Client-Cert": cert_pem})
        assert r.status_code == 401


def test_proxy_cert_with_wrong_secret_rejected(store, settings, fabric) -> None:
    _, cert_pem = fabric.accepted_device_cert(new_key())
    with _proxy_ingest_app(store, settings, fabric, client_addr=("203.0.113.7", 40000)) as c:
        r = c.post(
            "/ingest/event",
            json=_event_body(),
            headers={"X-Client-Cert": cert_pem, PROXY_AUTH_HEADER: "wrong-secret"},
        )
        assert r.status_code == 401


def test_proxy_authenticated_but_certless_rejected_even_on_loopback(store, settings, fabric):
    # Misconfigured optional-verify proxy: it authenticated the hop but forwarded no cert.
    # Must 401 and must NOT fall through to the loopback exemption.
    with _proxy_ingest_app(store, settings, fabric, client_addr=("127.0.0.1", 40000)) as c:
        r = c.post("/ingest/event", json=_event_body(), headers={PROXY_AUTH_HEADER: PROXY_SECRET})
        assert r.status_code == 401


def test_proxy_mode_local_certless_loopback_still_allowed(store, settings, fabric) -> None:
    # A genuinely local, non-proxied, certless caller (same-host poller) is still exempt.
    with _proxy_ingest_app(store, settings, fabric, client_addr=("127.0.0.1", 40000)) as c:
        r = c.post("/ingest/event", json=_event_body())
        assert r.status_code == 202


def test_proxy_mode_certless_non_loopback_rejected(store, settings, fabric) -> None:
    with _proxy_ingest_app(store, settings, fabric, client_addr=("203.0.113.7", 40000)) as c:
        r = c.post("/ingest/event", json=_event_body())
        assert r.status_code == 401


def _proxy_feedback_client(store, settings, fabric, *, client_addr, secret=PROXY_SECRET):
    settings = settings.model_copy(
        update={"require_mtls": True, "proxy_shared_secret": secret}
    )
    runtime = CadenceRuntime(
        config=RuntimeConfig(governor_mode="live"),
        settings=settings,
        store=store,
        device_verifier=fabric.verifier,
    )
    return TestClient(runtime.app, client=client_addr)


def test_feedback_proxy_authenticated_cert_allowed(store, settings, fabric) -> None:
    _, cert_pem = fabric.accepted_device_cert(new_key())
    c = _proxy_feedback_client(store, settings, fabric, client_addr=("203.0.113.7", 40000))
    r = c.post(
        "/nudge/does-not-exist/feedback",
        json={"kind": "thanks"},
        headers={"X-Client-Cert": cert_pem, PROXY_AUTH_HEADER: PROXY_SECRET},
    )
    assert r.status_code == 404  # gate passed, unknown nudge


def test_feedback_proxy_copied_cert_without_secret_rejected(store, settings, fabric) -> None:
    _, cert_pem = fabric.accepted_device_cert(new_key())
    c = _proxy_feedback_client(store, settings, fabric, client_addr=("203.0.113.7", 40000))
    r = c.post(
        "/nudge/does-not-exist/feedback",
        json={"kind": "thanks"},
        headers={"X-Client-Cert": cert_pem},
    )
    assert r.status_code == 401


def test_feedback_proxy_authenticated_certless_rejected(store, settings, fabric) -> None:
    c = _proxy_feedback_client(store, settings, fabric, client_addr=("127.0.0.1", 40000))
    r = c.post(
        "/nudge/does-not-exist/feedback",
        json={"kind": "thanks"},
        headers={PROXY_AUTH_HEADER: PROXY_SECRET},
    )
    assert r.status_code == 401  # proxied-but-certless, no loopback fallthrough


# --------------------------------------------------------------------------- #
# Robustness — an unexpected error during verification fails closed (401, not 500)
# --------------------------------------------------------------------------- #


def test_verify_unexpected_error_becomes_verification_error(fabric, monkeypatch) -> None:
    # A CA-valid cert reaches the registry lookup, which blows up (e.g. a DB error). verify
    # must convert that into a DeviceVerificationError, never let it propagate.
    key = new_key()
    cert_pem = fabric.issued_cert(key)

    def boom(_fingerprint):
        raise RuntimeError("simulated registry/DB failure")

    monkeypatch.setattr(fabric.registry, "by_fingerprint", boom)
    with pytest.raises(DeviceVerificationError, match="unexpected error"):
        fabric.verifier.verify(cert_pem)


def test_ingest_verifier_internal_error_is_401_not_500(store, settings, fabric, monkeypatch):
    key = new_key()
    cert_pem = fabric.issued_cert(key)

    def boom(_fingerprint):
        raise RuntimeError("simulated registry/DB failure")

    monkeypatch.setattr(fabric.registry, "by_fingerprint", boom)
    settings = settings.model_copy(update={"require_mtls": True})
    app = create_app(pipeline=IngestPipeline(store), settings=settings, verifier=fabric.verifier)
    with TestClient(app, client=("127.0.0.1", 40000)) as c:
        r = c.post("/ingest/event", json=_event_body(), headers={"X-Client-Cert": cert_pem})
        assert r.status_code == 401  # fail closed, not a 500
